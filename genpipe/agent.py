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

  * A second node, `ask_user`, is spliced in beside the gate and works the same
    way: it fires for an `ask(...)` call the model puts in an <execute> block,
    interrupts, and comes back with the person's answer as an <observation>.
    Riding inside <execute> is what makes it free -- Biomni's generate node
    routes it to "execute" like anything else, and our router picks it off
    there, so generate is never touched. See gate.ask_request.

A conversation is a thread; a run is a name
-------------------------------------------
  These were once the same string, which meant every message started a new run
  and every run had to be named before it could be described. Now one thread
  carries a whole back-and-forth and mints a run name at the gate, from what the
  run turned out to be. A conversation can therefore produce several runs, and
  each is approved, checked and diagnosed by its own name.

  The registry keeps both: `name` identifies the run forever, `thread_id` says
  which conversation produced it. held_for_thread() maps back the one time it
  matters -- a rejected run rethought on the same thread is the same run.

Driving the gated graph (in this file):
  run(prompt, thread_id) adds one turn to a conversation and streams until the
  graph finishes or stops for a human. resume(name, approved, feedback)
  continues a run paused at the gate by streaming Command(resume={...}). Both go
  through _drive(), which answers any ask-interrupts in place -- those are a
  question, not a decision, and returning to the command loop for each one would
  turn a conversation back into a form -- and stops only for the gate. Both end
  by calling _gate_status(), which reads the checkpoint and reports "paused"
  (with the proposal from _build_proposal) or "done". These replace Biomni's
  stock go(), which streams once in values mode and cannot detect or resume an
  interrupt.

Where the rest of the logic lives:
  Two stdlib-only modules hold everything that does not need a graph, so both
  are testable without installing biomni (which is what lets CI check them on
  every push):

    gate.py  the gate's decision logic -- is this code a submission, and
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
import contextlib
import io
import os
import re
import sqlite3
import uuid
import json
import datetime
from . import display
from . import gate
from . import intake
from . import preflight
from . import runs as runs_store
from . import slots as slot_table
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
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Command


# Distinguishes "the caller did not say" from "the caller said None", which for
# a run name is the difference between "work it out" and "this is not a hold".
_UNSET = object()

# Biomni's A1 was built to drive an analysis to completion: its system prompt
# pushes <execute> for everything and never says when NOT to. Here that default
# is wrong twice over. Most of what a GenPipes user types is talk -- a greeting,
# "what does -t do", "stringtie or cancer for my samples" -- and answering those
# with a shell block makes the tool feel like it isn't listening. Worse, A1's
# instinct after generating anything is to run it, so a message that asked for
# nothing at all could still walk a command up to the approval gate.
#
# So this says the quiet part out loud: talking is the default, and running
# something is what you do when you were asked to.
TALK_PROTOCOL = """

WHAT YOU ARE HERE
You are a GenPipes assistant for one person working on a Slurm cluster. Your job
is the whole arc of their pipeline work: answering questions about GenPipes,
preparing a run, submitting it once they approve, and watching it afterwards. You
are not a general bioinformatics agent, and the analysis is not yours to do.

Two modes, and you are in the first one unless told otherwise:

1. TALKING. Greetings, questions about GenPipes, "which protocol fits my data",
   "what does -t do", "what went wrong with that job" -- you answer these
   yourself, in a <solution> block, in your own words, with no <execute> at all.
   Answer at the length the question deserves and then stop and wait. This is
   most of what you will be asked.

2. DOING. They want a pipeline prepared, submitted, or monitored. Now you use
   <execute>: build the command, ask them for anything genuinely missing (see
   below), generate the script, submit it. Expect the submission to stop for
   their approval -- that stop is the design, not a failure.

HOW A RUN IS APPROVED -- read this twice
When they have asked for a run, carry it through to the submission in the SAME
turn: generate the script, then emit the submission command in an <execute> block
of its own.

You are not submitting when you do that. The submission block never reaches a
shell: it is intercepted and turned into an approval box showing the exact command
and the run's name, and that box is the only way they can say yes. So:
- Do NOT stop after generating to ask "shall I submit?", and do NOT say "let me
  know when you want me to submit". Neither of you can approve anything in prose,
  and it leaves them with nothing to approve -- no box, no run name, no record.
  It is the one way to make a run unapprovable.
- Do NOT use ask() for permission to submit. ask() is for facts you are missing.
  Approval is not a fact and has its own mechanism.
- Once the script exists, propose the submission. If they wanted a script and not
  a run, they will reject it, which costs one keystroke.
- Do not print or cat the generated script to prove it worked. The approval box
  states what will run, and the file is theirs to read.

The rules that keep the two apart:
- EVERY reply must contain exactly one <solution> or one <execute> block. A reply
  with neither is rejected unread and you are asked to redo it, which wastes
  their turn. If you have nothing to run, you are talking: use <solution>.
- Do not generate or submit anything nobody asked for. "hi", "thanks", and a
  question about a flag are not requests to build a pipeline.
- Never explore the environment for something to do. No surveying the
  installation, no `--help` you were not sent for, no data lake, no tool library
  -- none of that is what this tool is for, and it fills their screen with output
  they did not ask for. When you are idle, be idle.
- NEVER search the filesystem. No `find /`, no walking home directories, no
  hunting through /project or /scratch. Their files are in the working directory,
  and you are told what is there. If something they named is not there, say so
  and ask where it is -- a filesystem search is slow, enormous, and answers a
  question they can answer in four words.
- But working with their files IS the job when they ask for it. Read a readset,
  count the samples in it, check a column, fix a header, move or rename something,
  write a short script to answer a question about their data. Python is the
  default in an <execute> block and the interpreter keeps its variables between
  blocks; put "#!BASH" on the first line to use a shell instead. Work in the
  working directory, keep it small, and say what you did. The two rules above
  forbid wandering off unasked -- they do not forbid doing what was asked.
- If you cannot tell whether they want to talk or to run something, ask them
  (see below). Never resolve that doubt with a submission.
- Do not write numbered checklists or restate your plan. They are not shown, and
  what you are doing is already visible in the block underneath. Say the one
  sentence that explains the next action, then take it.

MONITORING
Reading the scheduler is free and ungated -- squeue, sacct, GenPipes' own
log_report, the .o log of a failed job. Use <execute> for those whenever they ask
how something is going, and answer from what came back rather than guessing.
Their own runs are named, and they can type /list, /check <name>, /jobs <name>
and /why <name> at any time; mention the one that fits when it saves them asking
you.
"""

# The one thing the model has to be taught about talking to the person at the
# keyboard. Deliberately short, and deliberately more about restraint than about
# syntax: a model that can open a menu will open one, and an agent that asks
# three questions before doing anything is a form with extra steps.
#
# The prohibition on listing options is the load-bearing line. The panel's
# choices are built from slots.PIPELINES, so a model that writes its own list
# gets ignored -- but a model that believes it is choosing the options will
# phrase its prose around ones that do not exist.
ASK_PROTOCOL = """

Asking the person at the keyboard a question:
When you genuinely cannot proceed without something only they can tell you, ask
for it with an ask() call alone in an <execute> block:

<execute>
ask(slot="protocol", pipeline="dnaseq")
</execute>

Their answer comes back to you as an <observation>. The slots that get a proper
choice panel are: pipeline, protocol, readset, design, pairs. Pass pipeline= and
protocol= when you know them, so the question can name what it is about. Do NOT
write out the available options -- they are filled in from this tool's own
tables, and any list you write would be ignored or, worse, wrong.

For anything with no slot of its own, ask in your own words:

<execute>
ask(question="Which steps should this run -- all of them, or a range?")
</execute>

When to ask, and when not to:
- Ask only when the answer changes what you would do and you cannot get it
  yourself. Reading a file, listing a directory, or running
  `genpipes <pipeline> --help` is not a question -- do that instead.
- Never ask for something already stated in the conversation, and never ask the
  same thing twice. If they declined to answer, proceed with a sensible default
  and say plainly which one you chose.
- Ask one thing at a time. Do not stack an ask() next to other code; a block
  containing anything besides the ask() call will be run as code instead.
- A question is not approval. Submitting is always gated separately, however
  many things they have confirmed along the way.
"""


# What the graph says to the model to get it moving again when its own last
# message left the conversation on the assistant's side. Recognised by
# display.parse as machinery, so it is never drawn as if the user typed it.
NUDGE = "[continue]"


def _call(node, state):
    """Run a graph node whether it arrived as a Runnable or a plain function."""
    return node.invoke(state) if hasattr(node, "invoke") else node(state)


def _quiet(node):
    """Wrap a graph node so its stray printing never reaches the terminal.

    Biomni's run_bash_script prints a full traceback.print_stack() and the raw
    CompletedProcess object whenever a command exits non-zero (biomni/utils.py).
    A non-zero exit is completely ordinary here -- `genpipes rnaseq` without -c
    is how the agent discovers it needs one -- and everything worth seeing is
    already in the string the node returns, which comes back as an <observation>
    and gets drawn properly by display.render. The print is pure duplication.

    It is also destructive: it arrives from inside biomni's worker thread, on
    stderr, which the spinner does not proxy, so a forty-line stack lands in the
    middle of the status line and tears the display apart. That is what this
    fixes -- suppressed here rather than patched upstream because biomni is a
    dependency, and because a node's return value is the only channel this
    application reads.
    """
    def quietly(state):
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            return _call(node, state)
    return quietly


def _shell_not_python(node):
    """Wrap the execute node so a shell command is run by a shell.

    Biomni chooses the interpreter from a marker on the block's first line --
    "#!BASH" or nothing, meaning Python (a1.py's execute node). The model writes
    that marker most of the time, and the time it forgets is the time it costs
    most: an approved `bash cmd.sh` was handed to exec() and came back as

        Error: name 'bash' is not defined

    after the person had already approved it. They then had to approve the retry.
    Nothing about that is recoverable by prompting harder -- one missing line in a
    block turns the one irreversible action in the tool into a no-op that looks
    like a failure of the cluster.

    So the marker is added here, from what the code actually is (see
    gate.mark_shell). It runs after the transcript has been drawn, so the
    person sees what the model wrote, and biomni sees what will work.
    """
    def run_as_written(state):
        messages = state.get("messages") or []
        if messages:
            last = messages[-1]
            fixed = gate.mark_shell(str(last.content or ""))
            if fixed != last.content:
                messages[-1] = type(last)(content=fixed)
        return _call(node, state)
    return run_as_written


def _observation_from_the_machine(node):
    """Wrap the execute node so its observation is a USER turn, not an assistant one.

    Biomni appends command output as `AIMessage("<observation>...")` (a1.py's
    execute node). That is a lie about who spoke, and with Anthropic it is a fatal
    one: a conversation ending in an assistant message is an assistant PREFILL --
    "here is the start of your reply, continue it" -- and every Claude model from
    Opus 4.7 onward refuses outright:

        BadRequestError: 400 ... This model does not support assistant message
        prefill. The conversation must end with a user message.

    So the first command the agent ran killed the turn. Relabelling the message
    fixes the shape and the semantics at once: tool output is something the
    environment hands the model, which is precisely a user-role turn, and it is
    what every other agent framework does with it.

    Only the trailing observation is touched, and only its role -- the text is
    byte-identical, so gate.py still reads the same history and the transcript
    still shows the same OUT block.
    """
    def run_and_relabel(state):
        state = _call(node, state)
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        if isinstance(last, AIMessage) and "<observation>" in (last.content or ""):
            messages[-1] = HumanMessage(content=last.content)
        return state
    return run_and_relabel


def _never_prefill(node):
    """Wrap the generate node so the model is never handed its own last word.

    The observation relabelling above fixes the case that happens on every run;
    this is the backstop for the rest. Biomni's generate routes a reply that
    contained only <think> straight back to itself, which leaves the assistant
    speaking last, and Anthropic rejects that conversation the same way (see
    _observation_from_the_machine). One neutral user turn is enough to make it
    legal, and NUDGE says nothing that could steer the answer.
    """
    def nudge_then_generate(state):
        messages = state.get("messages")
        if messages and isinstance(messages[-1], AIMessage):
            messages.append(HumanMessage(content=NUDGE))
        return _call(node, state)
    return nudge_then_generate


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

        # Biomni supports answering directly with <solution>, but never tells the
        # model when to prefer it over <execute>. These two append the missing
        # halves: when to just talk, and how to put a question to the user.
        self.system_prompt += TALK_PROTOCOL
        self.system_prompt += ASK_PROTOCOL
        # Who it is talking to. A1's prompt has no notion of a person on the
        # other end, and a conversational agent that cannot use your name is
        # oddly formal -- but one that opens every message with it is worse, so
        # the restraint is stated too.
        self._name_sentence = ""
        self.address_user(display.who())
        # 2. Borrow A1's real nodes from the compiled graph. No reimplementation.
        #    Each is wrapped, not replaced: _quiet swallows biomni's debug
        #    printing ("parsing error...", a traceback per non-zero exit), and the
        #    other two keep the conversation in a shape the Anthropic API accepts.
        generate = _quiet(_never_prefill(self.app.nodes["generate"].bound))
        execute = _quiet(_observation_from_the_machine(_shell_not_python(
            self.app.nodes["execute"].bound)))

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

        # 4. Routing out of generate: identical to A1 except that execute-bound
        #    code is first classified. Two kinds never reach the interpreter --
        #    a submission goes to the gate, an ask() goes to the question node --
        #    and everything else runs as usual.
        #
        #    The order of these two tests is a safety property, not a style
        #    choice. A block that both submits and asks is a submission; letting
        #    a stray ask() in the same block route it away from the gate would be
        #    a way to put work on the scheduler without approval.
        def routing_function(state):
            next_step = state.get("next_step")
            if next_step == "execute":
                code = self._extract_pending_code(state)
                if code and self._is_submission(code):
                    return "gate"
                if code and gate.ask_request(code):
                    return "ask"
                return "execute"
            if next_step in ("generate", "end"):
                return next_step
            raise ValueError(f"Unexpected next_step: {next_step}")

        # 5. Routing out of the gate: approve -> run it, adjust -> rethink.
        def gate_routing(state):
            return "execute" if state.get("next_step") == "execute" else "generate"

        # 5b. The question node. Same shape as the gate and for the same reason:
        #     everything before interrupt() must be recomputable, because a node
        #     re-runs from the top when the graph resumes.
        #
        #     The model names a slot; the options come from slot_table. That
        #     split is the whole design. A model asked to offer dnaseq protocols
        #     will eventually offer an eighth one and sound certain about it,
        #     whereas a model asked only *when* to raise the question cannot
        #     invent anything.
        def ask_user(state):
            request = gate.ask_request(self._extract_pending_code(state))
            gap = self._gap_for(request or {})
            if gap is None:
                # Nothing askable came out of the call -- a malformed ask(), or
                # a protocol asked of a pipeline that takes none. Say so in the
                # transcript rather than pausing on an empty panel.
                answer = None
                note = ("That question could not be put to the user. Proceed "
                        "without it, or ask something more specific.")
            else:
                # Plain data, never the Gap object: this payload is written into
                # the checkpoint by LangGraph's JSON serializer, which raises
                # "Object of type Gap is not serializable" and kills the turn
                # between deciding to ask and drawing the panel. See slots.as_data.
                answer = interrupt({"kind": "ask",
                                    "gap": slot_table.as_data(gap),
                                    "question": gap.question,
                                    "slot": gap.slot})   # <-- PAUSES here
                if isinstance(answer, dict):
                    answer = answer.get("answer")
                note = (f"The user answered: {answer}" if answer else
                        "The user declined to answer. Choose a sensible default, "
                        "state which one you chose, and carry on -- do not ask "
                        "again.")
            # Fed back as an <observation> because that is the shape the model is
            # already prompted to read after an <execute>. A bespoke format here
            # would be one more thing for it to learn for no gain.
            state["messages"].append(HumanMessage(
                content=f"<observation>{note}</observation>"))
            state["next_step"] = "generate"
            return state

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

        # 7. Rebuild the graph with the gate on the path to submission only, and
        #    the question node on the path to an ask() only.
        workflow = StateGraph(AgentState)
        workflow.add_node("generate", generate)
        workflow.add_node("execute", execute)
        workflow.add_node("submission_gate", submission_gate)
        workflow.add_node("ask_user", ask_user)
        workflow.add_conditional_edges(
            "generate", routing_function,
            path_map={"execute": "execute", "generate": "generate",
                      "gate": "submission_gate", "ask": "ask_user",
                      "end": END})
        workflow.add_conditional_edges(
            "submission_gate", gate_routing,
            path_map={"execute": "execute", "generate": "generate"})
        workflow.add_edge("ask_user", "generate")
        workflow.add_edge("execute", "generate")
        workflow.add_edge(START, "generate")

        self.app = workflow.compile(checkpointer=self.checkpointer)
    
    def address_user(self, name):
        """Tell the model what to call the person, replacing any earlier name.

        Swapped in place rather than appended, so /user mid-session leaves one
        name in the prompt instead of a growing list of everyone this session has
        claimed to be. configure() clears the record first, because A1.configure
        rebuilds system_prompt from scratch and the old sentence is gone with it.
        """
        previous = getattr(self, "_name_sentence", "")
        if previous and previous in self.system_prompt:
            self.system_prompt = self.system_prompt.replace(previous, "")
        self._name_sentence = (
            f"\n\nThe person you are talking to is called {name}. Use their name "
            f"when it is natural to -- not in every message." if name else "")
        self.system_prompt += self._name_sentence

    # --------------------------------------------------------------------- #
    #  Questions. The model decides when to ask; this decides what the       #
    #  question looks like, and the caller decides how it is rendered.       #
    # --------------------------------------------------------------------- #

    def _gap_for(self, request):
        """Turn a parsed ask() call into a slots.Gap the caller can render.

        The candidate files are read from the current working directory at the
        moment the question is asked, not from anything cached at startup. A
        conversation can run for an hour and a readset can arrive during it.
        """
        slot = request.get("slot")
        pipeline = request.get("pipeline")
        protocol = request.get("protocol")

        # A model that asks about a protocol without saying whose is common
        # enough to be worth recovering from, and the pipeline is nearly always
        # sitting in what the user just said.
        if slot in ("protocol", "design", "pairs") and not pipeline:
            pipeline = intake.find_pipeline(getattr(self, "user_task", "") or "")

        found = intake.candidates(os.getcwd())
        return slot_table.gap_for(
            slot,
            pipeline=pipeline,
            protocol=protocol,
            question=request.get("question"),
            readset_candidates=found["readset"],
            design_candidates=found["design"],
            pairs_candidates=found["pairs"],
        )

    def _answer(self, payload):
        """Put one question to the user and return their answer, or None.

        `on_ask` is set by whatever is driving the agent -- the command loop
        installs the terminal panel. When nothing is installed the answer is
        None, which the question node reads as "declined" and the model handles
        by choosing a default. That is the right behaviour for a scripted or
        headless driver: it must never be able to hang on a panel nobody can see.
        """
        asker = getattr(self, "on_ask", None)
        if asker is None:
            return None
        try:
            return asker(slot_table.from_data(payload["gap"]))
        except (EOFError, KeyboardInterrupt):
            return None

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
        A1.__init__ and again by add_software(), and because test_agent_gate.py builds
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
        """Drive the graph once, rendering every message as it arrives.

        Shared by _drive() and why() so the transcript looks the same regardless
        of which one produced it.

        Each message is drawn by display.render (the structured, coloured view)
        and also stored as plain text in self.log. pretty_print with
        printout=False still returns the formatted string, it just doesn't print
        it, so the log keeps a clean uncoloured copy of everything.

        The log is appended to, not reset: one turn can span several streams,
        because every question the agent asks ends one and the answer starts
        another. Callers that want a fresh log clear it first.

        on_step, if given, is called with each message -- used to drive the
        spinner's label from what the agent is actually doing, so a long run
        reports "running cmd.sh" rather than "thinking" for two minutes.
        """
        for s in self.app.stream(payload, stream_mode="values", config=config):
            msg = s["messages"][-1]
            # Resuming a pause replays the message the graph stopped on, and one
            # turn can contain several pauses -- every question the agent asks
            # ends a stream and the answer starts another. Without this the line
            # before each panel is printed twice, once on the way in and once on
            # the way back, which reads as the agent repeating itself. Tracked on
            # the instance, not locally, because the repeat spans two streams.
            seen = (type(msg).__name__, str(getattr(msg, "content", "")))
            if seen == getattr(self, "_last_rendered", None):
                continue
            self._last_rendered = seen
            display.render(msg)
            self.log.append(pretty_print(msg, printout=False))
            if on_step:
                on_step(msg)

    # A conversation that asked this many questions in a single turn has stopped
    # making progress. High enough never to be reached by a working agent,
    # finite so a loop cannot pin the user to a panel forever.
    MAX_QUESTIONS_PER_TURN = 12

    def _drive(self, payload, config, on_step=None):
        """Stream, answering questions in place, until the graph stops for good.

        The distinction this encodes is the one that makes the tool feel like a
        conversation. An ask-interrupt is a question: it belongs inside the turn,
        gets answered, and the agent carries straight on. A gate-interrupt is a
        decision with consequences on a shared cluster: it ends the turn, hands
        control back, and waits however long it waits.

        Both are the same LangGraph mechanism. Only the payload differs.
        """
        for _ in range(self.MAX_QUESTIONS_PER_TURN):
            self._stream(payload, config, on_step)
            value = self._interrupt_value(config)
            if not (isinstance(value, dict) and value.get("kind") == "ask"):
                return
            payload = Command(resume={"answer": self._answer(value)})

    def run(self, prompt, thread_id, on_step=None):
        """Add one turn to a conversation and drive it to a stop.

        thread_id names the CONVERSATION, not the run. It is the checkpoint key
        LangGraph saves state under, and it persists across turns so that this
        message arrives after everything already said rather than in front of a
        blank agent. Runs get their own names, minted at the gate -- see
        _settle().

        The history is replayed explicitly because Biomni's AgentState declares
        `messages` with no reducer, making it last-value-wins: passing only the
        new message would REPLACE the conversation instead of extending it. The
        alternative -- redeclaring the state with add_messages -- would change
        the semantics of a channel that /why deliberately relies on, so the
        replay stays here where it is visible.
        """
        config = self._config(thread_id)

        # A conversation parked at the gate cannot take another turn: streaming
        # fresh inputs would start a new superstep and discard the pending
        # interrupt, destroying an approval that is still outstanding. Callers
        # are expected to check first; this is the backstop that means a missed
        # check costs a refusal rather than a lost decision.
        value = self._interrupt_value(config)
        if isinstance(value, dict) and value.get("kind") != "ask":
            return self._gate_status(config)

        # Reset per-turn state, mirroring what go() does at the top of a task.
        self.critic_count = 0
        self.user_task = prompt
        self.log = []
        if getattr(self, "use_tool_retriever", False):
            selected = self._prepare_resources_for_retrieval(prompt)
            self.update_system_prompt_with_selected_resources(selected)

        prior = self._history(config)
        inputs = {"messages": prior + [HumanMessage(content=prompt)],
                  "next_step": None}

        # values mode gives no signal on a pause, so we don't inspect here;
        # _gate_status reads the checkpoint afterward to tell which happened.
        self._drive(inputs, config, on_step)
        return self._settle(thread_id, config, task=prompt)

    def resume(self, name, approved, feedback=None, on_step=None):
        """Continue a run paused at the gate. approved=True lets the command
        execute; approved=False sends feedback back to generate. Safe to call
        when nothing is paused: it just reports current status.

        `name` is the run's name, which is looked up in the registry to find the
        conversation it belongs to. That indirection is what lets one thread
        produce several runs and still be resumable by a name the user saw on
        screen days ago.

        On approval, the submission actually runs and GenPipes writes a job list
        file. That is the only moment the run's job list exists, so this is where
        the run's name is linked to that file for later progress lookups.
        """
        started = time.time()
        record = self.registry.get(name)
        if record is None:
            # Not a run name. Accept a conversation id too: it is what the
            # checkpoint is keyed by, so it still identifies the pause exactly,
            # and a registry that lost a record should not cost an approval.
            record = self.registry.held_for_thread(name)
            if record:
                name = record["name"]
        thread_id = (record or {}).get("thread_id") or name
        config = self._config(thread_id)
        snap = self.app.get_state(config)
        if not (snap.next and snap.tasks and snap.tasks[0].interrupts):
            if record is None:
                display.problem(f"No run named '{name}'.",
                                "/list shows what there is.")
                return {"status": "unknown", "thread_id": None}
            return self._gate_status(config)
        if approved:
            # Re-checked here, not just when the box was drawn. The box may have
            # been on screen for a day, and /approve is the irreversible act.
            blockers = self._blockers()
            if blockers:
                display.environment(blockers)
                display.problem(
                    "Not approved -- the environment would reject every job.",
                    "Nothing has reached the scheduler.")
                status = self._gate_status(config)
                status["blockers"] = blockers
                return status
        self.log = []
        command = Command(resume={"approved": bool(approved), "feedback": feedback})
        self._drive(command, config, on_step)
        if approved:
            self._record_submission(name, started)
        return self._settle(thread_id, config, held=None if approved else name)

    def _history(self, config):
        """Everything already said on this thread, or [] for a new one."""
        snap = self.app.get_state(config)
        return list((snap.values or {}).get("messages") or [])

    def spoken(self, thread_id):
        """Has anything been said on this conversation yet?

        Used to decide whether a message is an opening line, which is the only
        one that gets a brief attached to it.
        """
        return bool(self._history(self._config(thread_id)))

    def _settle(self, thread_id, config, task=None, held=_UNSET):
        """Classify where the turn ended up, record it, and show the consequence.

        Both run() and resume() end here so that reaching the gate always has the
        same three effects, in the same order, regardless of which call got
        there: the pause is persisted to the registry, the approval box is drawn,
        and the status dict is returned.

        Persisting BEFORE drawing is the point. Everything on screen is lost the
        moment the terminal closes; the record is what makes the pending decision
        findable again tomorrow.

        Naming happens here, and only here. A run is named once it exists -- once
        there is a command to look at -- rather than before the first word is
        typed, which is when nobody knows yet what they are naming.
        """
        status = self._gate_status(config)
        if status["status"] == "paused":
            proposal = status["proposal"]
            name = self._run_name(thread_id, proposal, task, held)
            self.registry.hold(name, thread_id, proposal, os.getcwd())
            if task:
                self.registry.update(name, task=task)
            blockers = self._blockers()
            status["blockers"] = blockers
            status["name"] = name
            display.gate(proposal, name, blockers=blockers)
        return status

    def _run_name(self, thread_id, proposal, task, held=_UNSET):
        """What to call the run now sitting at the gate.

        Reaching the gate twice on one thread is nearly always the same run
        being rethought after a rejection, so it keeps its name -- a second name
        for one pending decision would leave a phantom in /list that can never
        be approved. `held` overrides the lookup when the caller already knows
        which run this is, which resume() does and a fresh turn does not.

        Otherwise the name is derived from the command itself, falling back to
        the request that produced it. The command is the better source: it says
        `rnaseq -t stringtie` where the request said "the usual thing on Marie's
        samples".
        """
        if held is not _UNSET:
            if held:
                return held
        else:
            existing = self.registry.held_for_thread(thread_id)
            if existing:
                return existing["name"]

        slots = (proposal or {}).get("slots") or {}
        seed = " ".join(str(slots.get(k)) for k in ("pipeline", "protocol")
                        if slots.get(k))
        return self.registry.unique_name(
            runs_store.suggest_name(seed or task or "run"))

    def _blockers(self):
        """Environment problems that would make this submission fail regardless
        of the command.

        Checked here rather than only at startup because the gate is the last
        moment before anything is spent, and because a session can outlive the
        environment it started in.
        """
        if os.environ.get("GENPIPE_FAKE"):
            return []          # the fake cluster has no allocation to bill
        return preflight.blockers()

    def _interrupt_value(self, config):
        """The payload of whatever the graph is currently parked on, or None.

        Where a pause's payload lives is the one non-obvious part of LangGraph
        0.3.18's snapshot API: not on the snapshot directly, but on the pending
        task's interrupt. snap.next names the node the graph is parked before,
        and snap.tasks[0].interrupts[0].value holds what was passed to
        interrupt(). That exact path was confirmed empirically for this version;
        snap.interrupts does not exist. Checking all three guards avoids an
        IndexError when tasks is empty on a finished run.
        """
        snap = self.app.get_state(config)
        if snap.next and snap.tasks and snap.tasks[0].interrupts:
            return snap.tasks[0].interrupts[0].value
        return None

    def _gate_status(self, config):
        """Read the checkpoint after a turn ends and classify it: paused at the
        submission gate (returning the proposal to approve), still holding an
        unanswered question, or finished (returning the final message). Both run
        and resume call this so every outcome is reported through one consistent
        status dict.
        """
        snap = self.app.get_state(config)
        thread_id = config["configurable"]["thread_id"]
        value = self._interrupt_value(config)

        # A question still on the table after _drive() gave up asking. Reported
        # as its own status rather than dressed up as a proposal: the gate
        # renderer would draw an approval box for something that is not a
        # submission, and an approve line for a command that does not exist.
        if isinstance(value, dict) and value.get("kind") == "ask":
            return {"status": "asking",
                    "question": value.get("question"),
                    "thread_id": thread_id}

        if value is not None:
            return {"status": "paused",
                    "proposal": value,
                    "thread_id": thread_id}

        # Otherwise the run finished. snap.values can be empty for a thread that
        # never ran (unknown thread_id), so guard before indexing messages and
        # return final=None rather than raising.
        msgs = snap.values.get("messages") if snap.values else None
        return {"status": "done",
                "final": msgs[-1].content if msgs else None,
                "thread_id": thread_id}
    # --------------------------------------------------------------------- #
    #  Gate helpers. Thin delegates to gate.py, which holds the actual     #
    #  logic as pure functions over plain data.                               #
    #                                                                        #
    #  The split is not tidiness: gate.py imports nothing but the standard #
    #  library, so the one property that must never regress -- "does this     #
    #  code submit to a scheduler?" -- is checked on every push in seconds,   #
    #  without installing biomni. These methods stay because the graph's      #
    #  routing_function and test_agent_gate.py both call them, and delegating means #
    #  there is one implementation rather than two that can drift.            #
    # --------------------------------------------------------------------- #
    def _extract_pending_code(self, state):
        return gate.extract_pending_code(state.get("messages"))

    def _is_submission(self, code):
        return gate.is_submission(code)

    def _executable_lines(self, code):
        return gate.executable_lines(code)

    def _flag_value(self, cmd, flag):
        return gate.flag_value(cmd, flag)

    def _submission_line(self, code):
        return gate.submission_line(code)

    def _generation_command(self, state):
        return gate.generation_command(state.get("messages"))

    def _build_proposal(self, state, code):
        return gate.build_proposal(state.get("messages"), code)

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
        workdir = os.getcwd()

        # The proposal is read BEFORE the search, not after: it carries the -o
        # directory and the name of the script that was approved, and those are
        # what say where GenPipes put the list. The cwd is only where the person
        # was standing.
        held = self.registry.get(name)
        proposal = (held or {}).get("proposal")
        slots = (proposal or {}).get("slots") or {}
        newest = runs_store.find_job_list(workdir, since,
                                          output_dir=slots.get("output_dir"),
                                          script=(proposal or {}).get("script"))

        # Promote even when there is no list: "every step already up to date"
        # produces no jobs, and that is a successful outcome, not a run still
        # awaiting approval.
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
            # Two different things, and saying the wrong one costs a real
            # investigation: GenPipes may genuinely have created no jobs, or it
            # may have written a list somewhere the search did not reach. The
            # record cannot tell them apart after the fact, so say both and hand
            # over the escape hatch rather than assert the happier one.
            display.problem(f"No job list was recorded for '{name}' -- either "
                            f"every step was already up to date, or GenPipes "
                            f"wrote its list outside the directories searched.",
                            f"/track {name} <path/to/job_list>")
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
        self.log = []
        self._drive({"messages": [HumanMessage(content=prompt)], "next_step": None},
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
