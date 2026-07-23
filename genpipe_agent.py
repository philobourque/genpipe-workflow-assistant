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

Monitoring a submitted run:
  On approval, resume() records the run's thread_id against the job list file
  GenPipes just wrote (runs.jsonl). check(thread_id) resolves that link and runs
  GenPipes' log_report to report the run's progress.

Run tracking (runs.jsonl):
  One JSON record per submission: name, thread_id (None for a manually tracked
  run), job_list path, submitted_at, source ("agent" or "manual"), and a "gone"
  flag. submissions() shows only live records, checking each job_list path on
  disk first and quietly dropping ones that no longer exist -- no message, they
  just stop appearing. Nothing is ever deleted, though: history() shows every
  record, live or gone, so a run can still be found after Rorqual's copy of its
  artifacts is cleaned up. track(name, job_list_path) adds a record for a run
  launched outside the agent, with no thread_id behind it.
"""
import os
import re
import sqlite3
import uuid
import json
import datetime
import display
import time

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
    #  Run tracking store: one JSON record per submission, in runs.jsonl.   #
    #  A record carries a "gone" flag rather than ever being deleted, so a  #
    #  submission whose job_list file has vanished from Rorqual (scratch    #
    #  purge, manual cleanup) can drop out of submissions() silently while  #
    #  still being visible through history().                              #
    # --------------------------------------------------------------------- #
    def _runs_path(self):
        return os.path.join(self.path, "runs.jsonl")

    def _load_runs(self):
        path = self._runs_path()
        if not os.path.exists(path):
            return []
        records = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def _save_runs(self, records):
        # Write to a temp file and rename over the original so a crash mid-write
        # never leaves runs.jsonl half-written.
        path = self._runs_path()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        os.replace(tmp, path)

    def _add_run(self, name, job_list, thread_id=None, source="agent"):
        records = self._load_runs()
        records.append({
            "name": str(name),
            "thread_id": thread_id,
            "job_list": job_list,
            "submitted_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "gone": False,
        })
        self._save_runs(records)

    def _prune_runs(self, records):
        """Mark records whose job_list file no longer exists as gone, in place.
        A record already marked gone is not re-checked -- once the file is gone
        it stays gone, so a transient filesystem hiccup can't flicker it back to
        live. Returns True if anything changed, so the caller knows to save."""
        changed = False
        for r in records:
            if not r["gone"] and not os.path.exists(r["job_list"]):
                r["gone"] = True
                r["gone_at"] = datetime.datetime.now().isoformat(timespec="seconds")
                changed = True
        return changed

    def track(self, name, job_list_path):
        """Register a run launched outside the agent -- no thread_id, no prior
        conversation -- so check()/submissions()/history() can find it by name
        just like an agent-launched run."""
        job_list_path = os.path.abspath(job_list_path)
        if not os.path.exists(job_list_path):
            print(f"\n  '{job_list_path}' does not exist -- nothing tracked.\n")
            return
        self._add_run(name, job_list_path, thread_id=None, source="manual")
        print(f"\n  Tracking '{name}' -> {job_list_path}\n")

    def submissions(self):
        """List every LIVE recorded submission: the run's name, and the job list
        it points at. A record whose job_list file no longer exists is pruned
        first, silently -- see history() to find it anyway.

        Only approved submissions and track()'d runs appear here. A run that
        was rejected, or that only generated a script, never produced a job
        list and so was never recorded -- this is a record of pipelines on the
        cluster, not of conversations with the agent. Paused conversations live
        in the checkpoint, which is a separate store answering a different
        question."""
        records = self._load_runs()
        if self._prune_runs(records):
            self._save_runs(records)
        seen = {}
        for r in records:
            if not r["gone"]:
                seen[r["name"]] = r["job_list"]   # last live match wins per name
        if not seen:
            print("\n  No submissions recorded yet.\n")
            return
        display.submissions(seen)

    def history(self):
        """List every recorded run, live and gone, newest first. Unlike
        submissions(), nothing is hidden -- this is how you find a run again
        after its job_list file has been cleaned up from Rorqual."""
        records = self._load_runs()
        if self._prune_runs(records):
            self._save_runs(records)
        if not records:
            print("\n  No runs recorded yet.\n")
            return
        display.history(records)
# --------------------------------------------------------------------- #
    #  Driving layer: gated replacements for A1.go that survive the pause.   #
    # --------------------------------------------------------------------- #
    
    def run(self, prompt, thread_id):
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
        config = {"recursion_limit": 500,
                  "configurable": {"thread_id": str(thread_id)}}
        inputs = {"messages": [HumanMessage(content=prompt)], "next_step": None}

        # Drive the graph to completion or to the gate's pause. values mode gives
        # no signal on a pause, so we don't inspect here; _gate_status reads the
        # checkpoint afterward to tell which happened.
        #
        # Each message is drawn by display.render (the structured, coloured view)
        # and also stored as plain text in self.log. pretty_print with
        # printout=False still returns the formatted string, it just doesn't print
        # it, so the log keeps a clean uncoloured copy of everything.
        self.log = []
        for s in self.app.stream(inputs, stream_mode="values", config=config):
            msg = s["messages"][-1]
            display.render(msg)
            self.log.append(pretty_print(msg, printout=False))

        # If the run stopped at the gate, draw the approval box before returning,
        # so the decision and the commands to answer it are the last thing on
        # screen rather than something the caller has to go print themselves.
        status = self._gate_status(config)
        if status["status"] == "paused":
            display.gate(status["proposal"], status["thread_id"])
        return status

    def resume(self, thread_id, approved, feedback=None):
        """Continue a run paused at the submission gate. approved=True lets the
        command execute; approved=False sends feedback back to generate. Safe to
        call when nothing is paused: it just reports current status.

        On approval, the submission actually runs and GenPipes writes a job list
        file. That is the only moment the run's job list exists, so this is where
        the run's thread_id is linked to that file for later progress lookups.
        """
        started = time.time()
        config = {"recursion_limit": 500,
                  "configurable": {"thread_id": str(thread_id)}}
        snap = self.app.get_state(config)
        if not (snap.next and snap.tasks and snap.tasks[0].interrupts):
            return self._gate_status(config)
        command = Command(resume={"approved": bool(approved), "feedback": feedback})
        for _ in self.app.stream(command, stream_mode="values", config=config):
            pass
        if approved:
            self._record_submission(thread_id,started)
        return self._gate_status(config)

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
    #  Helpers. Pure logic, callable in isolation — this is the surface your #
    #  gate-invariant tests should hammer directly.                         #
    # --------------------------------------------------------------------- #
    def _extract_pending_code(self, state):
        """The code the model just proposed, pulled from the last message."""
        msgs = state.get("messages")
        if not msgs:
            return None
        last = msgs[-1].content
        if "<execute>" in last and "</execute>" not in last:
            last += "</execute>"
        m = re.search(r"<execute>(.*?)</execute>", last, re.DOTALL)
        return m.group(1) if m else None

    def _is_submission(self, code):
        """True only for code that actually submits: bash cmd.sh, or the DRAC
        chunk_genpipes.sh / submit_genpipes path. Generation and reads return False.

        KNOWN LIMITATION (observed 2026-07-13): these patterns are searched across
        the whole block as plain text, so they cannot tell a command that runs from
        a command that is merely printed. Both of these match:

            bash dnaseq_cmd.sh            <- actually submits
            echo "  bash dnaseq_cmd.sh"   <- prints the words, runs nothing

        This produced a live false positive: after an approved submission, the model
        wrote a summary of what it had done, that summary echoed the submission
        command, and the gate caught the summary as though it were a second
        submission. Rejecting it only made the model summarize again, so the run
        could not leave the gate.

        The fix is to match only text that actually executes. Matchees against
        _executable_lines(code) rather than the raw block, so a block that only prints
        a submission command, such as the model summarizing work it already finished does 
        NOT fire the gate.

        """
        if not code:
            return False
        code = self._executable_lines(code)
        patterns = [
            r"\b(?:bash|sh)\s+\S+\.sh\b",   # bash <script>.sh — any generated submission script
            r"\bsubmit_genpipes\b",            # DRAC submit
            r"\bchunk_genpipes\.sh\b",         # DRAC chunk (precedes submit)
        ]
        return any(re.search(p, code) for p in patterns)

    def _flag_value(self, cmd, flag):
        m = re.search(rf"{re.escape(flag)}\s+(\S+)", cmd)
        return m.group(1) if m else None

    def _submission_line(self, code):
        """Pull the real submission command out of a noisy code block, even if
        the model buried it in a Python script or a string literal."""
        if not code:
            return ""
        for p in (r"(?:bash|sh)\s+\S+\.sh\b",
                  r"chunk_genpipes\.sh\b[^\"'\n]*",
                  r"submit_genpipes\b[^\"'\n]*"):
            m = re.search(p, code)
            if m:
                return m.group(0).strip()
        return code.strip()

    def _generation_command(self, state):
        """Find the genpipes generation command anywhere in the history, since
        the model often generates in one block and submits in a later one."""
        for m in state.get("messages", []):
            content = getattr(m, "content", "")
            for block in re.findall(r"<execute>(.*?)</execute>", content, re.DOTALL):
                if "genpipes" in block and "-g" in block:
                    return block
        return ""

    def _build_proposal(self, state, code):
        """The payload shown to the human. The submission line is extracted from
        the caught block; the descriptive slots are parsed from the earlier
        generation command so the box stays populated even when the model splits
        generation and submission across separate blocks. Every fact is parsed
        from command text, never from the model's prose, so the explanation
        cannot disagree with the box the user approves."""
        cmd = self._submission_line(code or "")
        gen = self._generation_command(state)
        src = gen or (code or "")
        protocol = self._flag_value(src, "-t")
        steps = self._flag_value(src, "-s")
        inis = re.findall(r"\S+/([^/\s]+\.ini)", src) or re.findall(r"\S+\.ini", src)
        design = self._flag_value(src, "-d")
        pairs = self._flag_value(src, "-p")
        lines = ["About to submit this GenPipes run:", f"  command: {cmd}"]
        if protocol:
            lines.append(f"  protocol: {protocol}")
        if steps:
            lines.append(f"  steps: {steps}")
        if inis:
            lines.append(f"  config layering: {' , '.join(inis)}")
        if design:
            lines.append(f"  design file: {design}")
        if pairs:
            lines.append(f"  pairs file: {pairs}")
        lines.append("Approve to submit, or request an adjustment (protocol, steps, config).")
        return {
            "command": cmd,
            "explanation": "\n".join(lines),
            "slots": {"protocol": protocol, "steps": steps, "inis": inis,
                      "design": design, "pairs": pairs},
        }

    def _record_submission(self, thread_id, since):
        """Called from resume() right after an approved submission runs. Records
        the job list GenPipes just wrote, linking this run's thread_id to it so the
        run can be found again by name.

        `since` is the time the submission started. Only a job list written after
        that moment belongs to this run. Without this guard, a submission that
        creates zero jobs (everything already up to date) writes no list at all, and
        the glob would silently grab the newest list from a PREVIOUS run — linking
        this name to another run's jobs. The mtime filter prevents that: if nothing
        new was written, nothing is recorded."""
        import glob

        job_output = os.path.join(os.getcwd(), "job_output")
        lists = glob.glob(os.path.join(job_output, "*job_list*"))

        # Keep only job lists written by THIS submission, not a stale one.
        lists = [f for f in lists if os.path.getmtime(f) >= since]
        if not lists:
            return          # zero jobs created -> no list written -> record nothing

        newest = max(lists, key=os.path.getmtime)
        self._add_run(thread_id, newest, thread_id=str(thread_id), source="agent")

    def job_list_for(self, thread_id):
        """Look up the job list file recorded for a name (a thread_id, or a name
        given to track()). Returns the path, or None if there's no live record --
        a gone record is never returned, since there is nothing left to check."""
        records = self._load_runs()
        if self._prune_runs(records):
            self._save_runs(records)
        found = None
        for r in records:
            if r["name"] == str(thread_id) and not r["gone"]:
                found = r["job_list"]        # last live match wins
        return found

    def check(self, thread_id):
        """Show a submitted run's progress by its thread_id. Resolves the run's
        job list file, runs GenPipes' log_report on it, and draws the result.

        Prints rather than returns, because the point of it is to be looked at.
        The raw log_report text is still returned, so it can be captured."""
        import subprocess
        path = self.job_list_for(thread_id)
        if not path:
            print(f"\nNo recorded submission for '{thread_id}'.\n")
            return None
        cmd = ("module load mugqic/genpipes/6.1.1 && "
               f"genpipes tools log_report --loglevel ERROR {path}")
        out = subprocess.run(cmd, shell=True, capture_output=True,
                             text=True, executable="/bin/bash")
        raw = out.stdout + (("\n" + out.stderr) if out.stderr else "")
        display.status(thread_id, raw)
        return None

    def _executable_lines(self, code):
        """Return only the parts of a code block that actually run.

        A block can *mention* a submission without performing one. The model does
        this constantly when it summarizes or explains its own work, and it does
        it in two languages:

            bash dnaseq_cmd.sh                 <- runs: submits the pipeline
            echo "  bash dnaseq_cmd.sh"        <- shell: prints the words only
            print("  bash dnaseq_cmd.sh")      <- python: prints the words only

        All three contain the same text, so searching the raw block cannot tell
        them apart. Both false positives happened for real:

          2026-07-13, shell: after an approved submission, the model echoed a
          summary of what it had done. The gate caught the summary as a second
          submission, and rejecting it only produced another summary. Stuck.

          2026-07-14, python: on a generation-only task, the model ended with
          print("  bash launch_cmd.sh") inside a "here is how you would submit
          this" note -- while explicitly saying it was NOT submitting. The gate
          fired before the code ran, so the script was never even generated.

        So before asking "is this a submission?", strip the places text is written
        but never executed:

            - comments              (# anything)
            - echo arguments        (shell prints its words, it does not run them)
            - heredoc bodies        (cat << 'EOF' ... EOF is a block of plain text)
            - print() arguments     (python prints its words, it does not run them)

        What remains is only code that would truly execute, and _is_submission
        searches that instead of the raw block.

        WHY ONLY print(), AND NOT ALL PYTHON STRINGS. In python a quoted string is
        sometimes inert and sometimes the command itself:

            print("bash cmd.sh")                       <- inert
            subprocess.run("bash cmd.sh", shell=True)  <- a real submission

        Stripping every string would blind the matcher to the second, which is a
        false negative: a real submission slipping through ungated. That is the
        dangerous direction. A false positive only costs a rejection; a false
        negative costs an unapproved pipeline on the cluster. So this strips only
        what is handed to print(), the one construct we are confident is inert,
        and leaves every other string intact so subprocess and os.system calls are
        still caught.

        A rule of thumb, not a parser. A submission hidden in a variable that is
        never spelled out, an eval, or a $(...) would still slip through. That is
        accepted: the job is to stop misreading a cooperative model's own summary,
        not to defeat someone deliberately hiding a command.
        """
        # Nothing to look at.
        if not code:
            return ""

        kept = []            # the lines we decide are real code
        in_heredoc = False   # are we currently inside a block of heredoc text?
        end_word = None      # the word that will close that heredoc (often "EOF")

        # .splitlines() turns the one big block of code into a list of single lines,
        # so we can judge each line on its own.
        for line in code.splitlines():

            # .strip() removes spaces from both ends, so an indented "   EOF"
            # still matches the plain word "EOF".
            bare = line.strip()

            # --- Case 1: we are inside a heredoc -------------------------------
            # Everything here is text handed to a command, not code. Skip it. The
            # only thing we watch for is the closing word, which ends the block.
            if in_heredoc:
                if bare == end_word:
                    in_heredoc = False
                continue     # "continue" = skip to the next line, keep nothing

            # --- Case 2: does this line START a heredoc? -----------------------
            # A heredoc opens like:   cat << EOF    or    cat << 'EOF'
            # We grab the closing word so we know when the text block ends.
            opener = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", line)
            if opener:
                in_heredoc = True
                end_word = opener.group(1)   # group(1) = the captured word, e.g. EOF
                continue                     # the opener line is not a command

            # --- Case 3: a comment ---------------------------------------------
            # Anything starting with # never runs. Covers both shell and python.
            if bare.startswith("#"):
                continue

            # --- Case 4: strip shell echo's arguments --------------------------
            # echo only prints its words. Delete "echo" and everything after it,
            # but stop at ; | or & so a real command chained on the same line
            # survives:   echo hi && bash cmd.sh   ->   " && bash cmd.sh"
            line = re.sub(r"\becho\b[^;|&\n]*", "", line)

            # --- Case 5: strip python print's arguments ------------------------
            # print(...) only writes to the screen. Replace the whole call with an
            # empty print(), so anything quoted inside it -- including a submission
            # command the model is merely describing -- is gone. Strings passed to
            # subprocess, os.system, eval and friends are deliberately NOT touched:
            # those really do execute, and must still trip the gate.
            line = re.sub(r"\bprint\s*\([^)]*\)", "print()", line)

            # Whatever is left on this line is treated as real code.
            kept.append(line)

        # Join the surviving lines back into one block for the matcher to search.
        return "\n".join(kept)
