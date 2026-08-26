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
  diagnose(name, ...)   the only one that costs a model call. runs.triage() first
                   establishes deterministically WHICH jobs failed and reads
                   their logs, then the model is asked to explain the cause. It
                   runs on its own thread (see diagnose()'s docstring) so it can
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
from . import diagnosis
from . import provenance
from . import display
from . import capabilities as capability_table
from . import gate
from . import modify
from . import intake
# Aliased, because langgraph exports a function called `interrupt` -- the
# primitive the gate pauses with -- and this module is about ctrl-c. Two
# genuinely different things with one obvious name; the import below would
# otherwise shadow whichever came second, and it did.
from . import interrupt as interrupting
from . import override
from . import preflight
from . import runs as runs_store
from . import slots as slot_table
from . import telemetry
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

# The slots ask_user() will not let a declined question default its way past.
# Same set slots.gaps() ever raises a Gap for -- pipeline, protocol, and the
# three file roles -- as opposed to a free-form question like "which steps",
# where declining and defaulting is the whole point (see prep.ASSUMED).
_ESSENTIAL_SLOTS = {"pipeline", "protocol", "readset", "design", "pairs"}

# Re-exported from gate.py, which owns it -- see gate.REJECTION_MARK. Kept
# importable from here because this is where the sentence is rendered.
REJECTION_MARK = gate.REJECTION_MARK


class GatedState(AgentState):
    """Biomni's state, plus the one channel the gate needs of its own.

    `pending_proposal` is how a gate gets raised when there is no code block to
    build one from -- see regate(). It is TRANSPORT, not storage: the durable
    proposal lives on the registry record, which is what _perform_submission
    executes, and this channel exists only to carry it into the node that
    interrupts on it.

    That distinction is deliberate and worth keeping. A second durable copy of
    the proposal is exactly the parallel state this design is trying to remove;
    what is needed is a way to re-enter the gate node without asking a model
    for a code block, and one write-then-read channel is the whole of it.

    A subclass rather than an edit to biomni's TypedDict: its own nodes read
    only `messages` and `next_step` and are untouched by an extra key, while
    StateGraph reads the annotations to build its channels and needs to see
    this one.
    """

    pending_proposal: dict


# The whole system prompt, replacing A1's rather than appending to it.
#
# Biomni's A1 writes a prompt for a different product: a biomedical research
# agent with a 76-item data lake, a 113-entry library listing and a ~200-tool
# wet-lab registry, none of which exists here. The previous design kept that
# prompt and appended two documents arguing with it, which left the model
# reading three specifications at once and taking contradictory instructions
# from them:
#
#   the plan       A1 mandates a checklist marked [/] and [x] and says "always
#                  show the updated plan after each step". The appendix mandated
#                  [x] only, four-to-six stages, and "do not otherwise restate
#                  the plan in prose". display.py's renderer, meanwhile, matches
#                  only [ ], [x] and [v] -- so the failure mark A1 asks for does
#                  not render at all, and a plan that used it broke the block.
#   what to do     A1 pushes <execute> for everything and never says when not
#                  to. The appendix had to say talking is the default.
#   the resources  A1 advertises the tool library and data lake. The appendix
#                  forbade them. ~20 KB of prompt existed to be prohibited.
#
# One document, no appendix, no argument. The response contract below --
# <execute>, <solution>, <observation>, and the #!BASH marker -- is reproduced
# because A1's PARSER depends on those exact strings; everything else about A1's
# prompt is gone. genpipes.md is appended at configure() time as the grammar,
# and this document defers to it rather than restating it.
SYSTEM_PROMPT = """
You are the GenPipes assistant. One person, at a terminal, on a Slurm cluster on
the Digital Research Alliance of Canada. Your job is the whole arc of their
pipeline work: answering questions about GenPipes, working out what they want to
run, preparing it, submitting it once they have approved it, and watching it
afterwards.

You are not a general bioinformatics agent, and the science is not yours to do.
You are the colleague who knows GenPipes and this cluster well, sitting next to
them.


HOW YOU REPLY

Every reply contains exactly one <execute> block or exactly one <solution>
block. Never both, never neither -- a reply with neither is rejected unread and
you are asked to redo it, which costs them a turn.

  <solution>...</solution>   You are talking: answers, explanations, what you
                             found, what you are about to do.
  <execute>...</execute>     You are doing something. The block runs and its
                             result comes back to you as <observation>.

Inside <execute>, Python is the default and the interpreter keeps its variables
between blocks. Put #!BASH on the first line to use a shell instead, which is
what you want for anything involving GenPipes, modules, or the scheduler.

Talking is the common case. Most of what you are asked is a question, and a
question deserves a <solution>, not a shell block.


HOW A <solution> IS PRINTED

Into a terminal, close to as you typed it. Three markers are understood and
rendered:

  **bold**        a term worth emphasising. Sparingly -- everything emphasised
                  is nothing emphasised.
  `code`          a flag, a path, a filename, a command, a protocol or step
                  name. Anything they would type or look for on disk.
  - a bullet      one per line, at the start of the line.

Everything else prints as the characters you wrote. Headings, tables and
nested lists have no rendering here, so a `##` heading arrives as literal
hashes -- write a short plain label and a blank line instead. Put a runnable
command on a line of its own rather than in a fenced block; a fence is passed
through untouched, which is safe but noisy.

None of this is a template. Answer in whatever shape the question deserves --
a sentence, a paragraph, a short list -- and use the markers only where they
earn their place.


HOW YOU WORK

Like a competent colleague at a terminal, not like a form.

FIND OUT RATHER THAN ASK. If the answer is in a file, in a directory they
named, or in a --help you can run in two seconds, get it yourself and say what
you found. Asking a question you could have answered by looking is the most
irritating thing this tool can do -- above all when they have just told you
where the data is.

ASK when the answer is genuinely theirs to give: which of three protocols they
want, which of two readsets is this experiment, whether that really is the right
design file. Those are real questions. "Which readset file?" asked while holding
the folder it is sitting in is not.

Those two pull in opposite directions and the resolution is this: LOOKING IS HOW
YOU NARROW A QUESTION, NOT HOW YOU AVOID ONE. Go and find out, then either you
have a single unambiguous answer -- use it and say so -- or you have a real
choice to put to them. What you must not do is resolve a genuine ambiguity
silently and carry the guess all the way to an approval box. See "PREPARING A
RUN" for what has to be settled before you propose anything.

LEAD. They arrive with an intention, usually stated as a result rather than as a
command line -- "a quick AmpliconSeq test on the CIT data", "find inherited
variants in these samples". Work out what that means in GenPipes terms, say what
you understood, and carry it forward. Do not make them assemble a run one field
at a time; that is a form with extra steps, and they can already write the
command by hand.

Resolve things in the order that makes the next question answerable: what they
are trying to find out, then the pipeline, then the protocol, then the documents
that protocol requires, then anything they asked for explicitly. A protocol
decides whether a design file or a pairs file is even needed, so asking for one
before the protocol is settled is asking a question whose follow-up you cannot
predict.

Never ask about the step range, the cluster ini, or the output directory unless
they raised them. A run with no -s runs every step, and the ini for the machine
you are on is not a decision.


FINDING THINGS OUT

`genpipes <pipeline> --help` is authoritative and free. It prints the complete
flag set, the legal -t protocol values, the full numbered step list for every
protocol, and what each step does. It is version-exact, because it is the
install talking about itself. Read it before generating a command for a
pipeline, and never state a step number from memory -- the grammar document
below contains none, by design. If a generation is rejected for an out-of-range
step, re-read --help before changing anything else.

Its FIRST line is the one people skip and it is the one that decides whether a
command runs at all. argparse prints the required options bare and wraps every
optional one in brackets:

    usage: genpipes ampliconseq [-h] [--clean] -c CONFIG [CONFIG ...]
                                [-o OUTPUT_DIR] [-s STEPS]
                                -r READSETS_FILE [-d DESIGN_FILE]

So -c and -r are mandatory here and -d is not. Read that line before writing a
generation command, and put every unbracketed flag on it. A command missing one
is refused before it reaches the gate, so the cost of skipping this is a whole
turn spent being told what --help would have said for free.

Bracketed does not mean unnecessary. -d is optional to argparse and mandatory
for any run that includes the step which reads it -- argparse checks the command
line, not the pipeline. The grammar document below covers what --help cannot.

Reading costs nothing and needs no permission: --help, ls, hostname, reading a
readset or an ini, squeue, sacct, log files. Generation costs nothing either.
Use them freely, and in the same turn as the work they serve rather than as a
turn of their own.

The grammar document below is the basis -- invocation shape, config layering,
file formats, what is true of every run. Where it and --help disagree on
anything version-specific, --help wins.

Two things are not free:

  Submitting.  Gated. See below.
  Wandering.   Do not `find /`, do not walk home directories, do not survey the
               installation, do not go hunting through /project or /scratch for
               something nobody pointed at. Following a path somebody handed you
               is not wandering -- reading a directory they named is exactly
               what you should do. Searching for one they never mentioned is.

Never read the contents of a FASTQ, BAM, CRAM, VCF or result file. Names, sizes,
line counts and structure are enough.


ASKING THEM SOMETHING

When you genuinely need something only they can tell you, put the question in an
<execute> block of its own:

<execute>
ask(slot="protocol", pipeline="dnaseq")
</execute>

The slots that get a proper choice panel are pipeline, protocol, readset, design
and pairs. Pass pipeline= and protocol= when you know them, so the question can
name what it is about. Do NOT write out the available options -- they are filled
in from this tool's own tables, and any list you write would be ignored or,
worse, wrong.

THE WORDING IS YOURS. Add question= to a slot ask whenever you can put it
better than a generic form would, and you usually can, because you know what
this conversation is about and a fixed phrase does not:

<execute>
ask(slot="protocol", pipeline="dnaseq",
    question="You have matched normals, so this is a paired somatic run. Quick pass, or the ensemble?")
</execute>

The options underneath stay exactly what the tables say, so a better question
cannot introduce a protocol that does not exist. Leave question= off and you
get the plain form, which is fine when the plain form is what you meant.

For anything with no slot of its own, ask in your own words:

<execute>
ask(question="Which steps should this run -- all of them, or a range?")
</execute>

Their answer comes back as an <observation> and you carry straight on in the
same turn.

  - One question at a time, and nothing else in the block. A block containing
    anything besides the ask() call is run as code instead.
  - Never ask for something already stated in the request, already resolved by
    the context appended below their message, or already answered earlier in the
    conversation.
  - Never ask the same thing twice.
  - Do not ask permission to submit. Approval is not a fact you are missing; it
    has its own mechanism.
  - Look first, then ask what looking could not settle. "Could not settle"
    includes finding several plausible answers -- that is the case the panel
    exists for, not a case for picking one.


WHEN THEY CANNOT ANSWER

"idk", "not sure", "you pick", "whatever the test data uses" -- these are not
values. Never record one as a filename, a path, a protocol or a pipeline.

They mean you asked the wrong question, or asked it too early. Recover: say what
you were trying to establish, look where it could plausibly be, and come back
with either the answer or a better question. If they named a dataset rather than
a path -- "the CVMFS test data", "the CIT set" -- that is a location you can
resolve. Go and resolve it, then tell them what you are using.

If it truly cannot be resolved and it is required, say plainly that the run
cannot be prepared without it, and stop until they bring it up again. Do not
guess a readset, a design, a pairs file, a protocol or a pipeline. A guessed
protocol produces a run that completes successfully and answers a different
question, which is worse than one that fails.


WHEN THE REQUEST IS UNCLEAR

If you cannot tell whether they want to be told something or want something run,
ask -- one short question, in a <solution>. Never resolve that doubt by
submitting, and never resolve it by starting a questionnaire.

When they describe a scientific goal rather than naming a pipeline, say what you
took it to mean before acting on it: "that's dnaseq -t germline_snv -- inherited
SNVs and small indels". One sentence, and it is the cheapest correction there
is: seeing it wrong is what makes somebody say so before a generation is spent
on it.


PREPARING A RUN: SETTLE IT BEFORE YOU BUILD IT

Looking things up NARROWS a question. It does not skip it. For each input the
run needs, in this order -- pipeline, protocol, readset, then whatever that
protocol also requires (design, or pairs) -- go and look, then:

  exactly one candidate, and    use it, and say plainly which file you used and
  nothing about it is in doubt  where it came from
  more than one candidate       ask(slot=...). This is a real question and the
                                panel is how to put it
  none found                    say where you looked, then ask(slot=...)
  they named it outright        use it; do not ask again

Anything they asked for explicitly must actually appear on the command line. If
they said "steps 1-4", `-s 1-4` is on it. If they said "a new output directory",
`-o <that directory>` is on it -- creating a directory and writing the script
into it is not the same as passing it to GenPipes, and a run that quietly writes
somewhere else is exactly the kind of surprise this tool exists to prevent. If
you cannot honour something they asked for, say so and ask; do not drop it.

BEFORE YOU PROPOSE A SUBMISSION, ALL OF THIS MUST BE TRUE:

  - the pipeline and protocol are settled
  - every file the protocol requires is a real path you have confirmed exists,
    and it is either one they named or one they picked in a panel
  - everything they explicitly asked for is on the command line
  - the command was generated with -g and the script exists

If any of it is not true, you are not ready to propose. Ask for the missing
piece instead -- one thing at a time, through ask(), in the order above. The
approval box is for deciding whether to SUBMIT a finished command. It is not a
place to discover that the readset was never chosen, and a box carrying a hole
in it wastes the one decision you are asking them to make.


SUBMITTING IT

Two blocks, in this order, never one. First the generation, on its own, and you
wait for it to come back:

<execute>
#!BASH
genpipes ampliconseq -c ... -r ... -d ... -o $OUT -g $OUT/ampliconseq_cit_cmd.sh
</execute>

Then, once you have SEEN it succeed, the proposal, on its own:

<execute>
propose_submission("$OUT/ampliconseq_cit_cmd.sh")
</execute>

The path in propose_submission must be the SAME STRING you passed to -g,
character for character. They are two names for one file, and a box that
describes one while pointing at the other is the one thing this screen exists to
prevent.

Never put the generation and the proposal in the same block. A block containing
a proposal is intercepted whole and nothing in it runs, so a generation written
beside one is silently discarded -- you would be proposing a script that was
never written.

propose_submission does not submit. It is intercepted and turned into an
approval box showing the exact command and the run's name, and that box is the
only way they can say yes. Approval is typed by them, never inferred, and no
prose from either of you can stand in for it. When they approve, the generation
command is run again from the record and the script it produces is launched --
so what runs is exactly what the box described.

  - Do NOT stop after generating to ask "shall I submit?", and do not say "let
    me know when you want me to submit". Neither of you can approve anything in
    prose, and it leaves them nothing to approve -- no box, no run name, no
    record. It is the one way to make a run unapprovable.
  - Once the script exists, propose it. If they wanted a script and not a run,
    they reject it, which costs one keystroke.
  - If the generation FAILED, do not propose anything. Say what GenPipes said
    and fix it. A proposal whose script is not on disk is refused before it
    reaches them, and you will simply be asked to do this instead.
  - Do not print or cat the generated script to prove it worked. The box states
    what will run, and the file is theirs to read.


WHEN YOU ARE REBUILDING A RUN, SAY WHAT YOU CHANGED

If this proposal rebuilds a run that already exists -- a rerun, a variant, a
change somebody asked for at the gate -- declare the change on the same call:

<execute>
propose_submission("$OUT/cmd.sh", changes=[
    {"field": "config", "operation": "remove", "value": "override_walltime.ini"}
])
</execute>

Each entry is three things:

  field      the part of the command: pipeline, protocol, steps, design, pairs,
             readset, output, config
  operation  "set" for all of those except config, whose operations are "add",
             "remove" and "reorder"
  value      the new value for "set"; the ini for "add" and "remove"; the whole
             -c stack, in order, as a list, for "reorder"

  changes=[{"field": "steps", "operation": "set", "value": "1-4"}]
  changes=[{"field": "config", "operation": "add", "value": "dnaseq.exome.ini"},
           {"field": "config", "operation": "remove", "value": "cit.ini"}]

WHAT THIS IS FOR, because it is not paperwork. The application checks your
declaration against the command you actually generated and tells the person at
the gate when the two disagree. A change that was asked for and quietly did not
survive the regeneration is the failure this screen exists to catch, and it is
invisible unless something states what was supposed to happen.

`changes=[]` IS A REAL ANSWER and you must give it when you are rebuilding a
run without changing anything -- rerunning exactly the same command. A rebuild
that declares nothing at all is treated as an incomplete action: the person is
warned that nothing checked it, and they have to approve twice.

DECLARE WHAT YOU DID, NOT WHAT YOU WERE ASKED. If you decided against a change
-- it would break the run, or you need to ask first -- do not declare it. Say so
in prose and ask. Declaring a change you did not make is worse than declaring
nothing, because it is checked, and it will come back marked as not applied.

Nothing else about this changes. You still decide what the request means, which
ini it refers to, and where an ini you add belongs in the layering. This only
writes that decision somewhere it can be checked.

Leave `changes` out entirely for a genuinely new run. A first proposal is not a
modification of anything.


AT THE GATE

While a run waits for approval, anything they type reaches you rather than the
scheduler. It may be a change, a question, a correction, or something about
nothing in particular. Read it for what it actually is:

  a change      "make it steps 1-4", "use the other readset" -- regenerate the
                command with the change and propose it again.
  a question    "why did you pick stringtie?", "what will this cost?" -- answer
                it. Then propose the SAME submission again, unchanged, so the
                box comes back and the run is still there to approve. Answering
                without re-proposing leaves them with nothing to say yes to.
  anything else deal with it, then put the run back in front of them.

A question is not a rejection. Never let a run quietly disappear because they
asked about it, and never read curiosity as consent.


WORKING WITH THEIR FILES

Reading a readset, counting its samples, checking a column, fixing a header,
renaming something, writing a short script to answer a question about their
data -- that is the job when they ask for it. Work in the working directory,
keep it small, and say what you did. The rule against wandering forbids going
off unasked; it does not forbid doing what was asked.


MONITORING

Reading the scheduler is free and ungated: squeue, sacct, GenPipes' own
log_report, the .o log of a failed job. Use <execute> for those whenever they
ask how something is going, and answer from what came back rather than from what
you expect.

Their runs are named, and the application can answer about them directly -- see
WHAT YOU CAN ASK THE APPLICATION FOR. When somebody asks you plainly how a run
is doing, or why one broke, finding out is the answer; telling them which
command to type instead is not. They can still type /list, /check <name>,
/jobs <name> and /diagnose <name> whenever they would rather, and it is worth
mentioning the one that fits when it would save them a sentence next time.


RESTRAINT

Do not generate or submit anything nobody asked for. "hi", "thanks", and a
question about a flag are not requests to build a pipeline. When you are idle,
be idle.
"""


# The progress checklist, back on.
#
# It was parked because the block arrived on screen TWICE: display.parse()
# lifted `1. [ ] ...` lines out of the reply and drew them as the "Plan" block
# that updates in place, but did not remove them from the prose it then
# printed, so the same stages appeared again as raw markdown underneath --
# once rendered, once literal. The note left here called that a rendering
# defect rather than a prompting one, and said the fix was for parse() to
# consume the lines it claims.
#
# That is what display._strip_plan now does, on both the solution path and the
# connective-prose path, off one shared _PLAN_LINE pattern. With the lines
# claimed in exactly one place, the checklist can go back in the prompt.
PLANS = True

PLAN_PROTOCOL = """

THE PLAN

If finishing what they asked for will take you more than one reply, your FIRST
reply opens with a numbered checklist, exactly this shape, one line each:

    1. [ ] read the ampliconseq step list
    2. [ ] locate the CIT readset
    3. [ ] generate the command

Re-emit the whole list at the top of each following turn with the finished
stages marked [x]. It is drawn as one block that updates in place, so
re-emitting it costs them nothing and is how they watch progress.

Four to six stages. Each a short verb phrase for an observable stage of the job
-- not "call squeue", which is mechanics, and not "help the user", which is the
task restated. Use only [ ] and [x]: no other mark renders, so a stage that
failed is described in your prose and the list is changed, rather than given a
symbol of its own.

Do not otherwise restate the plan. Below the list, say the one sentence that
explains the next action, then take it.

Judge "more than one reply" by the whole job they asked for, not by the step in
front of you. Preparing a run is always several replies -- you have to establish
the protocol, find the files it needs, generate the command, and bring it to the
gate -- so it gets a checklist on the first reply, even though the first thing
you actually do is one lookup.
"""

# Said only while PLANS is off. Stated rather than left unsaid, because A1's
# prompt trained this habit hard and "no instruction to do it" is not the same
# as "an instruction not to": the numbered checklist is what a model reaches for
# unprompted on any multi-step task.
NO_PLANS = """

NO PROGRESS CHECKLISTS

Do not open with a numbered checklist of stages, and do not re-emit one as you
go. No "1. [ ] read the step list". Say in one sentence what you are about to
do, then do it, and say what you found. Numbered lists are still fine when the
CONTENT is a list -- the steps a pipeline will run, the files you found, options
they are choosing between. What is not wanted is a running tally of your own
progress.
"""


# What the graph says to the model to get it moving again when its own last
# message left the conversation on the assistant's side. Recognised by
# display.parse as machinery, so it is never drawn as if the user typed it.
NUDGE = "[continue]"


def _call(node, state):
    """Run a graph node whether it arrived as a Runnable or a plain function."""
    return node.invoke(state) if hasattr(node, "invoke") else node(state)


def _quiet(node):
    """Wrap a graph node so its stray printing never reaches the terminal, and
    so ctrl-c reaps biomni's worker instead of orphaning it.

    The whole of it lives in genpipe/interrupt.py, which is stdlib-only and so
    testable without installing biomni. This is the binding to a graph node --
    the one thing that module cannot know about.
    """
    return interrupting.shielded(lambda state: _call(node, state))


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


def _timed(node, kind, telemetry):
    """Wrap a graph node so its wall time is recorded under `kind`.

    This is where "one full model inference per <execute> block" (or per
    execute round) becomes a number instead of an impression from reading a
    transcript -- see AGENT-FIXES.md and genpipe/telemetry.py. A no-op unless
    telemetry.enabled, so it costs nothing in normal use.
    """
    def timed(state):
        return telemetry.timed(kind, _call, node, state)
    return timed


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
    # Has this turn already let a submission through the gate? Declared on the
    # class so the gate node can read it on a graph that has never been driven
    # -- _drive() clears it at the top of every turn, and the answer before any
    # turn has started is "no". See submission_gate.
    _submitted_this_turn = False
    # Whether this turn ran a `genpipes ... -g` block. Read by cli._talk after
    # the graph stops, to tell a turn that did real work and produced no run
    # from one that was only ever talk. Per-turn, cleared in _drive.
    _generated_this_turn = False

    # Runs the model examined this turn, declared on the class so the gate can
    # read it on a graph that has never been driven. See _drive, which clears
    # it at the top of every turn.
    _runs_examined = frozenset()

    # Whether the model may call the read-only capabilities. See
    # _capability_names() for what "off" means -- the router branch is never
    # taken and the node is unreachable, so this is a real switch rather than a
    # suppressed feature.
    #
    # ON. The table holds nothing that mutates a run, the scheduler or the
    # registry: every entry queries state and shows it. Approval, cancellation,
    # holds, rejection and adoption are absent from the table entirely, so no
    # setting here can reach them.
    capabilities_enabled = True

    # How many capability calls one turn may make before the loop is treated as
    # stuck. Generous, because a real investigation legitimately chains a few
    # -- check a run, then look at its jobs, then read the logs -- and stingy
    # enough that a model calling the same thing forever ends with something a
    # person can read instead of a recursion limit.
    MAX_CAPABILITIES_PER_TURN = 8

    # How many times in one turn the gate may hand a proposal back to be fixed
    # before giving up and saying so. See send_back.
    MAX_GATE_RETURNS = 3
    _gate_returns = 0

    # --------------------------------------------------------------------- #
    #  Graph construction: reuse A1's nodes, splice in the submission gate.  #
    # --------------------------------------------------------------------- #
    def configure(self, self_critic=False, test_time_scale_round=0):
        if self_critic:
            raise NotImplementedError(
                "GenpipeA1's gated graph supports the standard (non-self-critic) "
                "loop only. Run with self_critic=False."
            )

        # 1. Let A1 compile its ungated graph into self.app. Its system prompt
        #    is built here too and then discarded below -- the nodes are what
        #    this call is for.
        super().configure(self_critic=False,
                          test_time_scale_round=test_time_scale_round)

        # 2. Replace A1's prompt outright. See SYSTEM_PROMPT for why appending
        #    to it stopped being viable: the two documents contradicted each
        #    other on the plan format, on when to run code, and on whether a
        #    tool library existed at all.
        #
        #    genpipes.md arrives via add_software() -- Biomni files it under
        #    "software" and calls configure() again -- so it is read back out of
        #    that registry and appended here as the grammar. Without this it
        #    would go out with A1's prompt, taking the one document that is
        #    actually about GenPipes with it.
        self.system_prompt = (SYSTEM_PROMPT
                              + (PLAN_PROTOCOL if PLANS else NO_PLANS)
                              + self._capability_prompt()
                              + self._grammar())
        # Who it is talking to. A1's prompt has no notion of a person on the
        # other end, and a conversational agent that cannot use your name is
        # oddly formal -- but one that opens every message with it is worse, so
        # the restraint is stated too.
        self._name_sentence = ""
        self.address_user(display.who())
        # 2. Borrow A1's real nodes from the compiled graph. No reimplementation.
        #    Each is wrapped, not replaced: _quiet swallows biomni's debug
        #    printing ("parsing error...", a traceback per non-zero exit), the
        #    other two keep the conversation in a shape the Anthropic API
        #    accepts, and _timed records how long each actually took (see
        #    genpipe/telemetry.py) -- a no-op unless telemetry is enabled.
        generate = _timed(
            _quiet(_never_prefill(self.app.nodes["generate"].bound)),
            "generate", self.telemetry)
        execute = _timed(
            _quiet(_observation_from_the_machine(_shell_not_python(
                self.app.nodes["execute"].bound))),
            "execute", self.telemetry)

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
            # Timed in place, once, so every write after every graph step --
            # not just the ones this file adds -- shows up as "checkpoint" in
            # telemetry.summary(). A no-op wrapper unless telemetry is enabled.
            if not getattr(self._gate_checkpointer, "_genpipe_timed", False):
                original_put = self._gate_checkpointer.put
                telemetry = self.telemetry

                def _timed_put(*args, **kwargs):
                    return telemetry.timed("checkpoint", original_put, *args, **kwargs)

                self._gate_checkpointer.put = _timed_put
                self._gate_checkpointer._genpipe_timed = True
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
            # Set by regate(), never by the model. It means "a proposal is
            # already in hand -- go and offer it" and is the only way into the
            # gate node that does not begin with a code block. Checked first
            # because it is unambiguous; is_submission keeps its priority over
            # everything the MODEL can produce, which is the property that
            # matters.
            if next_step == "regate":
                return "gate"
            if next_step == "execute":
                code = self._extract_pending_code(state)
                if code and self._is_submission(code):
                    return "gate"
                if code and gate.ask_request(code):
                    return "ask"
                # AFTER the two above, and that order is the safety property.
                # is_submission keeps first place over everything the model can
                # write, so a block that both submits and calls a capability is
                # a submission. ask keeps second place because it predates this
                # and its behaviour must not shift.
                #
                # self._capability_names() is empty until the table is wired
                # up, which makes this branch inert rather than absent -- the
                # mechanism can be reviewed and tested before anything can
                # reach it.
                if code and gate.capability_request(code, self._capability_names()):
                    return "capability"
                # Recorded on the way past, not acted on. A generation is
                # ungated and stays ungated; this only remembers that the turn
                # ran one, so that ending without a gate can be reported rather
                # than being the silence it was. Set before execution, so a
                # generation that FAILED still counts -- a turn that tried and
                # produced no run is exactly the case worth saying out loud.
                if code and gate.is_generation(code):
                    self._generated_this_turn = True
                return "execute"
            if next_step in ("generate", "end"):
                return next_step
            raise ValueError(f"Unexpected next_step: {next_step}")

        # 5. Routing out of the gate: approve -> run it, adjust -> rethink,
        #    and "end" for the one case that is neither -- a second proposal
        #    arriving in a turn that has already spent its approval. See
        #    submission_gate. The default stays `generate` rather than `end`,
        #    because an unrecognised next_step here must never be a silent exit.
        def gate_routing(state):
            step = state.get("next_step")
            if step in ("execute", "end"):
                return step
            return "generate"

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
                if answer:
                    note = f"The user answered: {answer}"
                elif gap.slot in _ESSENTIAL_SLOTS:
                    # Esc on pipeline/protocol/readset/design/pairs used to be
                    # read the same as declining a question about step range or
                    # cluster config -- "pick something sensible and carry on" --
                    # which is how a declined protocol question still produced a
                    # filled-in gate box (germline_snv, chosen by the model, never
                    # confirmed by anyone). These are the slots gate.build_proposal
                    # will refuse to approve without anyway, so a guess here would
                    # only be corrected later, at best -- do not guess it at all.
                    note = (f"The user declined to answer -- no {gap.slot} was "
                            f"given. This is required and must NOT be guessed or "
                            f"defaulted. Do not generate or submit anything for "
                            f"this run. Say plainly that you cannot prepare it "
                            f"without a {gap.slot}, and stop until they bring it "
                            f"up again.")
                else:
                    note = ("The user declined to answer. Choose a sensible "
                            "default, state which one you chose, and carry on "
                            "-- do not ask again.")
            # Fed back as an <observation> because that is the shape the model is
            # already prompted to read after an <execute>. A bespoke format here
            # would be one more thing for it to learn for no gain.
            state["messages"].append(HumanMessage(
                content=f"<observation>{note}</observation>"))
            state["next_step"] = "generate"
            return state

        # 5c. The capability node. Same shape as ask_user, and deliberately so:
        #     a call the model wrote, resolved by deterministic code, answered
        #     as an observation, and the same turn carries on.
        #
        #     WHAT THIS NODE DOES NOT DO. It does not read the conversation, it
        #     does not look at what the person typed, and it has no opinion
        #     about which capability any sentence corresponds to. Everything it
        #     acts on is in `request`, which gate.capability_request() parsed
        #     out of the model's own <execute> block. The model decided; this
        #     checks the decision is legal and carries it out.
        def capability(state):
            request = gate.capability_request(self._extract_pending_code(state),
                                              self._capability_names()) or {}
            name = request.get("capability")
            # BOUNDED, because capability -> generate -> capability is a cycle
            # with a model call in it. A real investigation chains a few of
            # these legitimately; a model that has decided to call the same
            # thing forever would otherwise end on a GraphRecursionError, which
            # is not something a person can act on. The same argument as
            # send_back's, for the same shape of loop.
            self._capability_calls = getattr(self, "_capability_calls", 0) + 1
            if self._capability_calls > self.MAX_CAPABILITIES_PER_TURN:
                state["messages"].append(HumanMessage(content=(
                    f"<observation>That is {self._capability_calls} lookups in "
                    f"one turn and the answer is not getting closer. Stop "
                    f"calling them and say what you have found, or what you "
                    f"are stuck on.</observation>")))
                state["next_step"] = "generate"
                return state
            args, more = capability_table.continues(request.get("args"))
            spec, complaint = capability_table.validate(name, request.get("args"))
            if complaint:
                # Refused before any handler sees it, and the refusal goes back
                # as an observation rather than as an exception -- a model that
                # got an argument wrong should be able to read why and try
                # again inside the same turn, which is how ask() already
                # behaves for a malformed question.
                note = complaint
            else:
                note = self._run_capability(spec, args)
            state["messages"].append(HumanMessage(
                content=f"<observation>{note}</observation>"))
            # WHERE THE TURN ENDS, and this is the whole of the fix for it.
            #
            # This node used to set "generate" unconditionally, and the edge
            # out of it was unconditional too, so EVERY capability -- including
            # the ones that had just drawn the complete answer on screen --
            # went back for another model call. The canonical panel was being
            # treated as an intermediate tool observation. Two things that
            # produced, both seen live:
            #
            #   list_runs drew twenty-one runs, and the model, handed an
            #   observation that named no runs, said "No runs currently
            #   recorded" underneath it.
            #
            #   a question about a failed run spent 36 model calls and 185
            #   seconds AFTER the last panel was rendered, wandering through
            #   inspect_jobs, show_run, where and run_history.
            #
            # A capability that ANSWERS the person ends the turn, unless the
            # model said in the call itself that it is not finished -- see
            # capabilities.continues. The observation is appended either way,
            # so the conversation remembers what happened and a later turn can
            # build on it; what does not happen is another generation in THIS
            # one.
            #
            # ends_turn, NOT renders, and the difference is a real defect that
            # reached a user. They were one field, so every capability that
            # drew a panel was treated as the end of the turn -- including
            # `where`, which is orientation. A model preparing a run asked
            # where the artifacts would land, the panel printed, and the turn
            # ended: no command generated, no gate, no run recorded, and
            # nothing on screen saying the work had been abandoned. Drawing a
            # panel and being the answer are separate claims and are separate
            # fields now.
            #
            # A complaint is never terminal: nothing was rendered, so the model
            # has to be given the chance to fix the call it wrote.
            done = spec is not None and spec.ends_turn and not more
            state["next_step"] = "end" if done else "generate"
            return state

        # 6. The gate node. Pure before interrupt() (safe to re-run on resume).
        def send_back(state, note):
            """Refuse to draw a box, and tell the model what to fix.

            The gate's way of saying "this is not a decision yet". It routes to
            generate rather than execute, so nothing is submitted on the way
            past, and it happens BEFORE the interrupt, so the node stays pure
            and safe to re-run when the graph resumes.

            BOUNDED, because the loop it sits in is not. generate -> gate ->
            send_back -> generate is a cycle with a model call in it, and the
            only thing that ever stopped it was LangGraph's recursion limit,
            which _config sets to 500. A model that cannot find the readset it
            is being asked for would burn hundreds of calls and end on a
            GraphRecursionError, which is not something a person can read. Three
            attempts is enough for a model that is going to get there; after
            that the turn ends and says what it was stuck on, which is a thing
            somebody can act on.
            """
            self._gate_returns += 1
            if self._gate_returns > self.MAX_GATE_RETURNS:
                display.problem(
                    "The command could not be made ready to submit.",
                    f"{note.rstrip('.')}. Nothing has reached the scheduler — "
                    f"say what to do next, or start again with /new.")
                state["next_step"] = "end"
                return state
            state["messages"].append(HumanMessage(content=note))
            state["next_step"] = "generate"
            return state

        def submission_gate(state):
            code = self._extract_pending_code(state)

            # ONE APPROVAL, ONE SUBMISSION, ONE TURN.
            #
            # A submission that fails comes back through execute to generate,
            # and the model's honest reaction to `No such file or directory` is
            # to fix the command and propose it again -- which arrived here, in
            # the same turn, and pauses on a second approval box.
            #
            # What that looked like from the outside: /approve, a spinner, and
            # then the same READY TO SUBMIT box that had just been approved,
            # with the amber "the submission failed" notice printed BELOW it,
            # because the box is drawn from inside resume() and the outcome is
            # reported by its caller afterwards. The whole screen read as
            # "/approve did nothing". It was worse than that -- see _settle's
            # `held` argument for the second name the same turn minted.
            #
            # So the turn ends here instead. Nothing is submitted (END is not
            # execute), the model's own account of the failure has already been
            # rendered on its way past, and the person is left with the amber
            # notice and a decision to make rather than with a box that has
            # quietly become a retry. Say "go on" and the re-proposal comes
            # back to the gate properly, in a turn of its own, with the failure
            # still on screen above it.
            if self._submitted_this_turn:
                state["next_step"] = "end"
                return state

            # From the code block when there is one, and from the channel
            # when this is a re-gate -- a decision being restored after a turn
            # that consumed it without replacing it. See regate() for why the
            # restoration is a real interrupt rather than a status word.
            if state.get("next_step") == "regate" and state.get("pending_proposal"):
                proposal = dict(state["pending_proposal"])
            else:
                proposal = self._build_proposal(state, code)

            # An incomplete proposal never reaches the person. The gate means
            # "you are one step from submitting"; a box you can only reject is
            # not that, and it puts the wrong job on the wrong party -- the
            # model knows what a readset is and can go and find one, whereas
            # the person is handed a form with a hole in it and no way to fill
            # it in from where they are standing.
            #
            # So it goes back to be finished, naming what is absent, and the
            # gate is drawn only when there is a decision to make. This is why
            # display.gate's `missing` branch and mirror._absent(required=True)
            # exist no longer: the state they rendered cannot be reached.
            #
            # Before the interrupt, so the node stays pure and safe to re-run
            # on resume, and routed to generate rather than execute so nothing
            # is submitted on the way past.
            missing = proposal.get("missing") or []
            if missing:
                return send_back(state, "That submission is not ready: "
                                 + ", ".join(missing)
                                 + " missing. Find or ask for what is missing, "
                                   "then regenerate the command and propose it "
                                   "again.")

            # AND SO IS A COMMAND THAT ARGPARSE ITSELF WILL NOT ACCEPT.
            #
            # `missing` above is slots.py's table talking: which FILES this
            # pipeline and protocol need. This is the install talking, about the
            # flags on the command line, and it catches a class the table
            # structurally cannot -- there is no entry for `-c` in slots.gaps(),
            # because which inis a run needs depends on the machine, so a
            # generation with no config stack at all satisfied every check and
            # was drawn as READY TO SUBMIT. Approving it spent nothing and
            # achieved nothing: genpipes exited on `the following arguments are
            # required: -c`, having been told so by its own --help all along.
            #
            # Named as flags, not as rows, because the model's job here is to
            # edit a command line. Silent when --help could not be read (see
            # gate.with_usage): an unreadable install is not evidence of a bad
            # command, and refusing on it would break the tool hardest on the
            # machines where it is least debuggable.
            lacking = proposal.get("lacking") or []
            if lacking:
                pipeline = ((proposal.get("slots") or {}).get("pipeline")
                            or "this pipeline")
                required = " ".join(proposal.get("required") or lacking)
                return send_back(state, (
                    f"That command is missing {', '.join(lacking)}. "
                    f"`genpipes {pipeline} --help` marks {required} as required "
                    f"and argparse will refuse the command without them. Add "
                    f"what is missing to the generation command, re-run it, and "
                    f"propose the submission again."))

            # AND THE SCRIPT HAS TO EXIST BEFORE ANYBODY IS ASKED ABOUT IT.
            #
            # Same rule as `missing`, applied to the artifact rather than to the
            # flags: a box drawn for a script that was never written is not a
            # decision, it is a form with a hole in it that only the model can
            # fill. It means the generation did not run, or ran and failed, or
            # wrote somewhere other than where the submission is pointing --
            # every one of which is the model's to fix and none of which the
            # person can do anything about from the gate.
            #
            # Checked only when it CAN be checked. runs.resolvable is false for
            # a path still holding a `$VAR`, because that variable is set by
            # `module load` in the shell that runs the command and not in this
            # process -- and "I cannot check this" must never be reported as "it
            # is not there". That confusion is exactly what refused a perfectly
            # good ampliconseq run whose script was sitting on disk the whole
            # time.
            declared = proposal.get("script")
            if declared and runs_store.resolvable(declared) and not (
                    runs_store.resolve_path(
                        declared, os.getcwd(),
                        (proposal.get("slots") or {}).get("output_dir"))):
                return send_back(state, (
                    f"{declared} does not exist. The submission names a script "
                    f"that has not been written: run the genpipes generation "
                    f"command with -g {declared} first, check it succeeded, "
                    f"and only then propose the submission."))

            reply = interrupt(proposal)                            # <-- PAUSES here

            # WHAT COMES BACK IS A DECISION A PERSON MADE, and it is reported
            # to the model as exactly that: an observation about what the
            # APPLICATION did, rendered from the structured record, never a
            # sentence written by hand and never an edit to anything the model
            # said. See _render_decision and registry.add_decision for the
            # failure that rule comes from.
            if reply.get("done"):
                # ALREADY SUBMITTED, OUTSIDE THIS GRAPH.
                #
                # resume() regenerates the script and launches it itself, as
                # two recorded commands with no model call between the person
                # saying yes and the jobs existing -- see _approve for why the
                # irreversible span is model-free. By the time the graph is let
                # go, the submission is over and reconciled.
                #
                # So there is nothing here to execute and nothing worth a model
                # call to say: display.post_approve has already printed the
                # reconciled outcome, and a turn that could only restate it,
                # slowly, would be spending an inference to add nothing.
                #
                # The observation is still appended, and that is the half that
                # matters later. The conversation now knows the submission
                # happened, so the next question about this run is answered
                # against what occurred rather than against a proposal the
                # model still believes is pending -- which is what produced
                # "let me know once you've approved it" from a model whose
                # submission had already run.
                self._submitted_this_turn = True
                state["messages"].append(HumanMessage(
                    content=self._render_decision(reply.get("decision"),
                                                  reply.get("name") or "this run")))
                state["next_step"] = "end"
                return state

            # An approval that did NOT come from _approve() cannot reach here.
            # It used to: the graph itself ran the submission, which is why
            # _make_runnable existed to rewrite the model's block into
            # something the interpreter would accept. Both are gone. If a
            # caller ever resumes with approved=True and no `done`, that is a
            # bug in the caller and the safe reading is "no decision was
            # taken" -- so it falls through to the not-approved branch rather
            # than submitting on an assumption.
            note = reply.get("feedback") or "Adjust the command before resubmitting."
            state["messages"].append(HumanMessage(
                content=self._render_decision(
                    {"decision": "rejected", "feedback": note,
                     "revision": proposal.get("revision")},
                    reply.get("name") or "this run")
                + "\nRegenerate the command accordingly, or answer the "
                  "question and propose the same submission again."))
            state["next_step"] = "generate"                    # loop back to rethink
            return state

        # 7. Rebuild the graph with the gate on the path to submission only, and
        #    the question node on the path to an ask() only.
        workflow = StateGraph(GatedState)
        workflow.add_node("generate", generate)
        workflow.add_node("execute", execute)
        workflow.add_node("submission_gate", submission_gate)
        workflow.add_node("ask_user", ask_user)
        workflow.add_node("capability", capability)
        workflow.add_conditional_edges(
            "generate", routing_function,
            path_map={"execute": "execute", "generate": "generate",
                      "gate": "submission_gate", "ask": "ask_user",
                      "capability": "capability", "end": END})
        workflow.add_conditional_edges(
            "submission_gate", gate_routing,
            path_map={"execute": "execute", "generate": "generate",
                      "end": END})
        workflow.add_edge("ask_user", "generate")
        # CONDITIONAL, where it used to be a plain edge to "generate". The node
        # decides; this is what lets its decision be honoured. Anything other
        # than the two it sets falls through to `generate`, for the same reason
        # gate_routing does: an unrecognised value must never be a silent exit.
        workflow.add_conditional_edges(
            "capability",
            lambda state: "end" if state.get("next_step") == "end" else "generate",
            path_map={"generate": "generate", "end": END})
        workflow.add_edge("execute", "generate")
        workflow.add_edge(START, "generate")

        self.app = workflow.compile(checkpointer=self.checkpointer)
    
    # _make_runnable() lived here, and its removal is the point rather than a
    # tidy-up.
    #
    # It rewrote the model's own <execute> block in place -- turning
    # `propose_submission("cmd.sh")` into `bash cmd.sh` -- so that biomni's
    # execute node, which re-reads the pending code out of the last MESSAGE,
    # would find something runnable. That was a real constraint, and the
    # solution was to edit history.
    #
    # The constraint is gone. _approve() runs both commands itself, outside the
    # graph, and releases the interrupt with a settled fact; the graph never
    # executes a submission any more, so nothing needs the block to be
    # runnable. The branch that called this became unreachable at that point
    # and is deleted with it.
    #
    # What it left behind was not harmless. The surviving transcript showed the
    # assistant running a script with no gate in it, and the model -- reading
    # its own history on the next turn -- apologised for bypassing an approval
    # that had been given. See registry.add_decision for the replacement: the
    # decision is stored as data and the sentence is rendered from it.
    #
    # THE RULE THIS ESTABLISHES: conversation history is never rewritten to
    # make an application-side action look like a model action. Anything the
    # application did is reported to the model as an observation about the
    # application, in words generated from state that can be checked.

    @staticmethod
    def _render_decision(entry, name):
        """The observation text for one gate decision. Generated, never stored.

        A projection of registry.add_decision's entry, rebuilt on every turn
        from the record. That is what keeps it from drifting: there is no
        sentence sitting in the history that can go on claiming something the
        record no longer says.

        Written to be unambiguous about the one thing the model kept getting
        wrong -- that the decision has already been made and the work is done
        -- because the raw output of a submission looks exactly like the
        output of any other script, and for a run with nothing to do it is
        nearly empty.
        """
        decision = (entry or {}).get("decision")
        revision = (entry or {}).get("revision") or "?"
        if decision == "approved":
            outcome = (entry or {}).get("outcome") or {}
            jobs = outcome.get("jobs")
            did = (f"{jobs} job{'s' if jobs != 1 else ''} were submitted"
                   if jobs else "no jobs were created — every step was "
                                "already up to date")
            return (f"<observation>\n"
                    f"THE USER APPROVED THIS SUBMISSION. The application then "
                    f"regenerated the script from the recorded command and ran "
                    f"it. Neither of those was your doing and neither needs "
                    f"your confirmation.\n"
                    f"  run:      {name}\n"
                    f"  revision: {revision}\n"
                    f"  result:   {outcome.get('status', 'unknown')} — {did}\n"
                    f"  detail:   {outcome.get('detail') or '-'}\n"
                    f"It is finished. Do not propose it again, do not ask for "
                    f"approval, and do not say it is waiting for one.\n"
                    f"</observation>")
        if decision == "rejected":
            feedback = (entry or {}).get("feedback") or ""
            return (f"<observation>\n"
                    f"The user {REJECTION_MARK} {name} "
                    f"(revision {revision}).\n"
                    f"They said: {feedback or '(nothing further)'}\n"
                    f"</observation>")
        return (f"<observation>\n"
                f"The user changed {name}; revision {revision} is superseded.\n"
                f"</observation>")

    def _grammar(self):
        """genpipes.md, as registered by cli.build_agent's add_software().

        Biomni has no notion of "a document the model should read"; the nearest
        thing it offers is the software registry, so the grammar is filed there
        and fished back out here. Returns "" before registration -- configure()
        runs once at construction, before add_software, and again from inside
        add_software once the grammar is in place.
        """
        entry = (getattr(self, "_custom_software", None) or {}).get("genpipes")
        text = (entry or {}).get("description") if isinstance(entry, dict) else entry
        if not text:
            return ""
        return ("\n\n\nTHE GENPIPES GRAMMAR\n\nWhat follows is the reference for "
                "this installation. It is the basis; --help is authoritative on "
                "anything version-specific.\n\n") + text

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

    # ------------------------------------------------------------------- #
    #  Capabilities: the actions the model may ask the application to run.
    #
    #  EVERY ENTRY IS BACKED BY THE METHOD THE SLASH COMMAND ALREADY CALLS.
    #  That is the rule, not a coincidence, and it is what makes "the two
    #  routes share an implementation" true rather than aspirational --
    #  cli._cmd_check is four lines around agent.check(), and so is this. A
    #  capability that did its own work would be a second implementation of
    #  something a person can also reach, and the two would drift.
    # ------------------------------------------------------------------- #
    def _capability_prompt(self):
        """The section of the system prompt that lists the available actions.

        WHAT THIS DELIBERATELY DOES NOT SAY. There is no line here of the form
        "if they ask about status, call check_run". That mapping is the model's
        to make, from the whole conversation, and writing it down would move
        the decision into a lookup table -- the same deterministic
        intent-routing this design refuses, relocated from Python into English
        where it would be harder to see and impossible to test.

        So the section states three things and stops: these actions exist, this
        is what each one does, and whether to use one is your call. The
        signatures and summaries are generated by capabilities.catalogue(), so
        the actions the model is told about and the actions that exist cannot
        diverge.

        Empty when capabilities are switched off, which keeps 7A's inert state
        genuinely inert -- a model told about calls it cannot make would try
        them and read an error.
        """
        if not self._capability_names():
            return ""
        return f"""


WHAT YOU CAN ASK THE APPLICATION FOR

Besides shell commands, there are things only this application can do -- it
holds the run registry, it knows which run has which job list, and it owns the
renderers people are used to reading. Those are available to you as calls.

Write one on its own in an <execute> block, exactly as you would an ask():

<execute>
check_run(name="ampliconseq-0813")
</execute>

{capability_table.catalogue()}

{capability_table.protocol()}

  - One call per block, and nothing else in the block.
  - These read state. None of them changes a run, a file or anything on the
    scheduler.
  - The run's NAME is what they take. If you do not know it, list_runs() shows
    what there is.

WHETHER TO USE ONE IS YOUR JUDGEMENT, and it is a judgement, not a rule. Some
things people say are requests to look at the state of their work; some are
questions about how this tool or GenPipes behaves; some are neither. Only the
first kind is a reason to call anything. A question about what a command does,
or about why jobs time out in general, is answered by explaining -- reaching
for a capability there would inspect a run nobody asked about and answer a
question nobody asked.

You are not obliged to use one because it exists. Answer from what you already
know when you already know it, ask when you are genuinely unsure which run is
meant, and look when looking is what would actually help.

ONE LOOKUP USUALLY ANSWERS ONE QUESTION. Each of these already shows the person
a full panel, so three of them is three screens to read for something they
asked once. Ask for another only when the first genuinely leaves something open
that the second can close -- and when it does, that is what `more=True` is for.

WHAT TO AVOID IS REPEATING AN IDENTICAL LOOKUP -- the same call, with the same
arguments, whose answer you already have. That shows them a panel they have
just read and tells you what you already know.

Calling the same one again with DIFFERENT arguments is an ordinary thing to do
and is often exactly right: "compare run-a and run-b" is two check_run calls,
one per run, and "the failed jobs in both" is two inspect_jobs calls. The test
is whether the next call can tell you something the last one did not.

THE SLASH COMMANDS STILL EXIST and are not going anywhere: /check, /jobs,
/diagnose, /monitor, /list, /view, /history. They are the same operations by
another door, for somebody who would rather type than ask. Mention one when it
saves them a sentence next time -- but when they have asked you plainly for
something you can do, do it rather than telling them what to type.
"""

    def _capability_handlers(self):
        """{name: callable}. Built per call so a swapped registry is honoured.

        Signatures take the argument names the table declares, so validate()
        and the handler cannot disagree about what a call looks like.
        """
        return {
            "check_run": lambda name=None: self.check(name),
            "inspect_jobs": lambda name=None, failed=False: self.jobs(
                name, only_failed=bool(failed)),
            "diagnose_run": lambda name=None, question=None: self.diagnose(
                name, question=question),
            "show_run": lambda name=None: self._show_run(name),
            "list_runs": lambda: self.submissions(),
            "run_history": lambda: self.history(),
            "where": lambda: self._where(),
        }

    def _capability_names(self):
        """The names the router will recognise, or an empty set when off.

        EMPTY IS THE INERT STATE and it is deliberate. With no names,
        gate.capability_request() returns None for everything, the router
        branch is never taken, and the node is unreachable -- so the mechanism
        can be built, reviewed and tested before any of it can affect a
        conversation. Switching it on is one flag, in one place.
        """
        if not getattr(self, "capabilities_enabled", False):
            return frozenset()
        return frozenset(self._capability_handlers()) & capability_table.NAMES

    def _run_capability(self, spec, args):
        """Execute one validated capability and describe what it produced.

        Two audiences, one action. The handler PRINTS -- the real panel, the
        same one the slash command draws, because a person watching should see
        what they would have seen either way. What comes back from here is a
        text rendering of the same facts for the model, so it can talk about
        them rather than re-deriving them.

        Neither is a summary of the other and neither is invented: both are
        made from the value the handler returned.
        """
        handler = self._capability_handlers().get(spec.name)
        if handler is None:                      # pragma: no cover - table drift
            return f"{spec.name} is not wired up here."
        wanted = {k: v for k, v in (args or {}).items() if k in spec.args}
        # WHICH EXISTING RUNS THE MODEL HAS LOOKED AT THIS TURN.
        #
        # Read at the gate to decide whether a proposal is a DERIVED run and so
        # owes a declaration of what it changes. "rerun Test_walltimefail
        # without override_walltime.ini" cannot be answered without looking that
        # run up, so the lookup is the reliable, prose-free signal that this
        # proposal is a rebuild of something rather than a fresh run.
        #
        # It records the model's OWN ACTION -- a capability call it chose to
        # write -- and never anything the user typed. Cleared at the top of
        # every turn by _drive, so it describes this turn only.
        # Only a name the registry actually knows. `check_run(name="all")` is a
        # documented call and names no run, so recording it would mark a turn
        # as a rebuild on the strength of somebody asking how their runs were
        # doing -- and the fresh run proposed afterwards would arrive at the
        # gate carrying a warning about a rebuild that never happened.
        asked = str(wanted.get("name") or "")
        if asked and asked != "all":
            try:
                known = self.registry.get(asked) is not None
            except Exception:                  # noqa: BLE001
                known = False
            if known:
                examined = set(getattr(self, "_runs_examined", None) or ())
                examined.add(asked)
                self._runs_examined = examined
        try:
            result = handler(**wanted)
        except Exception as e:                   # noqa: BLE001
            # A capability that fails is a fact about the run, not a reason to
            # end the turn. The model gets the failure and can say so.
            return (f"{spec.name} could not be completed: "
                    f"{type(e).__name__}: {e}")
        return self._capability_note(spec, wanted, result)

    def _capability_note(self, spec, args, result):
        """The observation text for a finished capability.

        TWO JOBS, AND THE SECOND ONE WAS MISSING AT FIRST. It has to carry
        enough of the finding for the model to reason about, and it has to make
        clear the work is DONE.

        The first version said only "the diagnosis was produced and shown to
        the user above" -- true, and content-free. A model given that has
        nothing to explain, so it called diagnose_run again looking for
        substance, and again: six capability calls for one question, three of
        them the expensive one, and three near-identical panels for the person.
        The fix is not a stricter limit. It is to answer the question the model
        was still asking.

        Still short. The full rendering is already on screen and repeating it
        here would spend context on something the person can see -- and would
        tempt the model to read numbers out of a table rather than out of a
        fact it was handed.
        """
        subject = args.get("name") or ""
        if result is None:
            # Every handler that can refuse has already said why on screen --
            # _need_run does it in four different wordings. Saying so plainly
            # keeps the model from narrating a result it did not get.
            return (f"{spec.name} could not answer for {subject or 'that'}; "
                    f"the reason was shown to the user.")

        done = "This lookup is finished and its output is already on screen."
        if spec.name == "diagnose_run" and isinstance(result, dict):
            return self._diagnosis_note(result, subject, done)
        if hasattr(result, "counts") and result.counts is not None:
            counts = ", ".join(f"{n} {s.lower()}"
                               for s, n in sorted(result.counts.items())) or "no jobs"
            facts = (f"{subject}: {result.verdict}. {counts}, "
                     f"of {result.total} submitted.")
            cause = getattr(result, "root_cause", None) or {}
            if cause.get("step"):
                facts += (f" Earliest independent failure: {cause['step']}"
                          + (f", {cause['count']} job(s) {str(cause.get('state') or '').lower()}"
                             if cause.get("count") else "") + ".")
            return f"{facts} {done}"
        if spec.name == "inspect_jobs" and isinstance(result, list):
            shown = "failed " if args.get("failed") else ""
            return (f"{subject}: {len(result)} {shown}job(s) listed for the "
                    f"user, grouped by step. {done}")
        if spec.name in ("list_runs", "run_history") and isinstance(result, list):
            # THE COUNT, because "was shown to the user" is content-free and a
            # model handed nothing will fill the space itself. It filled it,
            # live, with "No runs currently recorded" printed under a panel
            # holding twenty-one of them. What goes back now is what the panel
            # says, tallied from the same rows it was drawn from.
            if not result:
                return f"{spec.name}: there are no runs to show. {done}"
            tally = {}
            for row in result:
                # list_runs yields (record, status) pairs and gets the same
                # word the panel printed; run_history yields bare records and
                # has no scheduler status to offer, so it reports the recorded
                # one. Neither is invented and neither is re-derived.
                if isinstance(row, tuple):
                    word = runs_store.list_tag(row[0], row[1])
                else:
                    word = str((row or {}).get("status") or "unknown")
                tally[word] = tally.get(word, 0) + 1
            breakdown = ", ".join(f"{n} {w}" for w, n in sorted(tally.items()))
            return (f"{spec.name}: {len(result)} run(s) shown to the user "
                    f"({breakdown}). {done}")
        if spec.name in ("list_runs", "run_history", "where"):
            return f"{spec.name} was shown to the user. {done}"
        return f"{spec.name} completed for {subject or 'this session'}. {done}"

    def _diagnosis_note(self, result, subject, done):
        """What a diagnosis established, for the conversational model. Not a panel.

        WHY IT IS ITS OWN BRANCH. diagnose() returns a DICT -- the gate status,
        with the parsed answer and this run's scheduler facts on it -- and the
        note builder tested `hasattr(result, "counts")`, which a dict does not
        have. So the branch written for diagnose_run was unreachable and the
        most expensive lookup in the product reported itself as "diagnose_run
        completed for <name>". A model handed that and asked to carry on has
        nothing to carry on FROM, which is how one ended up speculating about a
        run underneath its own freshly rendered diagnosis.

        EVERYTHING HERE IS ALREADY IN HAND. The tally and the root cause were
        resolved before the model was called; the cause, the override and the
        caveats were parsed out of the answer it gave. Nothing is re-read, re-
        queried or re-derived -- this function has no scheduler, no log and no
        model in it, and could not acquire one.

        ONE RUN, NAMED. Every value comes out of `result["evidence"]`, which
        diagnose() built under the name it was given, or out of the parse of
        that run's own answer. There is no path by which another run's numbers
        could arrive here, because no other run is in scope.

        AND IT IS NOT A SECOND RENDERER. The screen has the full finding; this
        is a handful of sentences so a following turn can compare two runs, or
        answer a question about the one just diagnosed, without going and
        looking again. Long values are cut rather than wrapped -- nobody reads
        this, it is read.
        """
        evidence = result.get("evidence") or {}
        parsed = result.get("diagnosis") or {}
        name = evidence.get("name") or subject or "that run"

        out = [f"{name}:"]
        verdict = str(evidence.get("verdict") or "").strip()
        counts = evidence.get("counts") or {}
        if verdict:
            out.append(f"{verdict}.")
        if counts:
            tally = ", ".join(f"{n} {state.lower()}"
                              for state, n in sorted(counts.items()))
            out.append(f"{tally}, of {evidence.get('total')} submitted.")
        cause = evidence.get("root_cause") or {}
        if cause.get("step"):
            where = f"Earliest independent failure: {cause['step']}"
            if cause.get("count"):
                where += (f", {cause['count']} job(s) "
                          f"{str(cause.get('state') or '').lower()}")
            out.append(where + ".")

        # The model's own conclusion, as diagnosis.parse structured it. Absent
        # for an answer that came back unshaped, which is a real outcome and
        # says so by omission rather than by an empty heading.
        if parsed.get("cause"):
            out.append(f"Diagnosed: {_one_line(parsed['cause'], 220)}.")
        if parsed.get("override"):
            fix = "; ".join(
                f"[{step}] " + ", ".join(f"{k} = {v}" for k, v in
                                         sorted(settings.items()))
                for step, settings in sorted(parsed["override"].items()))
            out.append(f"Proposed override for {name}: {_one_line(fix, 200)}.")
        # THE CAVEATS TRAVEL WITH THE FIX, here as everywhere else. A value
        # nothing proved sufficient must not reach a later turn as a bare
        # recommendation -- see registry.remember_remediation for the same rule
        # on the durable side.
        uncertain = list(parsed.get("uncertain") or ())
        if uncertain:
            more = f" (+{len(uncertain) - 1} more)" if len(uncertain) > 1 else ""
            out.append(f"Not established: {_one_line(uncertain[0], 160)}{more}.")
        out.append(done)
        return " ".join(out)

    def _show_run(self, name):
        """/view's body, as a capability. Reconciles first, like the command."""
        record = self.registry.get(name)
        if record is None:
            display.problem(f"No run named '{name}'.", "/list shows what there is.")
            return None
        self.reconcile_registry([record])
        record = self.registry.get(name) or record
        proposal = record.get("proposal") or {}
        if not proposal:
            display.problem(f"'{name}' has no command on record.",
                            "It was adopted from a job list rather than built "
                            "here. /check tells you how it is doing.")
            return None
        display.run_view(
            proposal, name, record["status"],
            resources=override.summary(override.read(override.path_for(
                name, record.get("workdir") or ".", proposal))),
            blockers=self._blockers() if record["status"] == runs_store.HELD
            else (),
            record=record)
        return record

    def _where(self):
        """/where's body, as a capability."""
        here = preflight.cluster()
        rows = [
            ("cluster", f"{here}  ({preflight.cluster_ini()})" if here
             else "not a recognised Alliance login node"),
            ("launched from", os.getcwd()),
            ("agent workdir", self.path),
            ("run registry", self.registry.path),
            ("checkpoints", os.path.join(self.path, "genpipe_checkpoints.sqlite")),
        ]
        display.where(rows)
        return rows

    def _gap_for(self, request):
        """Turn a parsed ask() call into a slots.Gap the caller can render.

        The candidate files are read from the project directory named in what
        the user just said, at the moment the question is asked, not from
        anything cached at startup, and never from the process's own working
        directory (AGENT-FIXES.md defect 1) -- a candidate pulled from wherever
        this process happens to be running is a fabrication, not a fact about
        the user's data.
        """
        slot = request.get("slot")
        pipeline = request.get("pipeline")
        protocol = request.get("protocol")
        task = getattr(self, "user_task", "") or ""

        # A PIPELINE IS NEVER RECOVERED FROM THE USER'S WORDS HERE, and the
        # three lines that used to do it are the reason this comment is long.
        #
        # They read: if the model asked about a protocol, a design or a pairs
        # file without saying which pipeline it meant, scrape one out of what
        # the user last said (intake.find_pipeline). The intention was
        # recovery. The effect was the mention-is-selection defect that prep.py
        # was rewritten to delete, still live one module over -- measured on
        # the sentences that provoked that rewrite:
        #
        #     "should I use rnaseq or chipseq?"  ->  chipseq
        #     "I do NOT want chipseq"            ->  chipseq
        #
        # A comparison resolved to one of the things being compared and a
        # refusal resolved to the thing refused, and the panel then offered
        # that pipeline's protocols as though the question had been settled.
        # Matching a word proves it was typed, never that it was chosen.
        #
        # Nothing is needed in its place, which is the part worth knowing:
        # slots.gap_for() already handles an unnamed pipeline correctly and
        # says why in its own docstring -- asked for a protocol with no
        # pipeline, it returns the PIPELINE gap, because that is the real gap
        # and answering it first is what the ordering exists to enforce. So
        # removing the guess does not lose the recovery; it replaces a guessed
        # answer with the question that was actually outstanding.
        named = intake.find_directories(task)
        found = intake.candidates(named[0] if named else None)
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
    #  diagnose(). Everything else delegates.                                    #
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

    @property
    def telemetry(self):
        """Call/timing counters for the generate/execute loop -- see
        genpipe/telemetry.py. Off by default (GENPIPE_TELEMETRY=1 to enable);
        lazy for the same reason `registry` is: configure() runs during
        A1.__init__ and again from add_software(), and test_agent_gate.py
        builds a bare instance with object.__new__, no __init__ at all."""
        if not hasattr(self, "_telemetry"):
            self._telemetry = telemetry.Telemetry()
        return self._telemetry

    def track(self, name, job_list_path):
        """Register a run launched outside the agent -- no thread_id, no prior
        conversation -- so check()/list/history can find it by name just like an
        agent-launched run."""
        job_list_path = os.path.abspath(job_list_path)
        if not os.path.exists(job_list_path):
            display.problem(f"'{job_list_path}' does not exist -- nothing tracked.")
            return
        # Both refusals come back from the registry rather than being decided
        # here, so they hold wherever adoption is reached from -- this method,
        # /scan, or the capability layer that will eventually offer it in
        # prose. A guard in the CLI would have protected exactly one door.
        record, refused = self.registry.track(name, job_list_path)
        if refused:
            # The next step depends on WHICH of the two things was wrong, and
            # both arguments are typed by hand so either can be. Offering
            # "pick another name" for a file that is not a job list sends
            # somebody to fix the half that was fine.
            taken = self.registry.get(name) is not None
            display.problem(
                f"Not tracked -- {refused}",
                "Pick another name  ·  /list shows what there is." if taken
                else "Point it at the job_list GenPipes wrote — "
                     "job_output/<Pipeline>.<protocol>.job_list.<timestamp>.")
            return
        display.tracked(name, job_list_path)
        return record

    def submissions(self):
        """List every run still worth acting on: held runs awaiting a decision,
        and submitted runs whose artifacts are still on disk.

        A run whose job_list has vanished is pruned first, silently -- see
        history() to find it anyway. Held runs are listed even though nothing of
        theirs is on the scheduler yet, because a pending approval is the most
        actionable thing this tool can be holding.

        One batched scheduler round-trip (runs_store.resolve_all(), the same
        call /check all makes) covers every launched run, so LIVE means
        "queued or running right now" rather than whatever a stale cached
        verdict happened to say last time somebody typed /check. Held runs
        cost nothing here -- resolve_all() never builds a manifest for one, so
        there is nothing of theirs for the scheduler query to include.

        A successful resolution is cached the same way /check all caches one,
        so /check <name> benefits too. A run the scheduler could not be
        reached for is NOT cached over top of whatever was there before --
        losing a real, still-valid last known verdict to a transient query
        failure would be a worse outcome than the query failing in the first
        place.
        """
        # Reconciled BEFORE anything is rendered, not only at startup. A
        # session can outlive the state it started in -- a run approved in
        # another terminal, a proposal whose gate this session's own /modify
        # consumed -- and /list is where a stale claim does the most damage,
        # because it is the screen people act from. The pass is cheap: it
        # reads a checkpoint per pending record and asks the scheduler
        # nothing.
        self.reconcile_registry()
        records = self.registry.live()
        if not records:
            display.nothing("No runs recorded yet.",
                            "Describe a pipeline in plain English to start one.")
            # An EMPTY LIST, not None. This used to return None here and, worse,
            # from the bottom of the function as well -- so a screen showing
            # twenty-one runs came back to the model indistinguishable from
            # this one, and _capability_note's `result is None` branch told it
            # "list_runs found nothing to report". The model said so underneath
            # the twenty-one runs. Emptiness is a fact and it has a value.
            return []
        rows = runs_store.resolve_all(records)
        for record, status in rows:
            if (status is not None and status.total
                    and status.source != "unavailable"):
                self.registry.remember_check(record["name"], status.counts,
                                             status.total, status.verdict)
                self.registry.remember_reasons(record["name"], status.reasons)
        display.run_list(rows)
        return rows

    def history(self, name=None):
        """The archive of run records, newest first, or one record in full.

        Unlike submissions(), nothing is hidden and nothing is asked of the
        scheduler -- this is how you find a run again after its job_list file
        has been cleaned up from Rorqual, and it stays an archive rather than
        becoming a second, slower /list.

        `name` opens one record: its provenance and whatever /diagnose found
        out about it. Those findings used to be printed under every row of the
        summary, which is what made the summary unreadable; they are still
        recorded, and this is where they are read.
        """
        records = self.registry.all()
        if not records:
            # [] rather than None, for the reason submissions() gives: a
            # capability that rendered an empty archive and one that could not
            # answer at all must not come back looking the same.
            display.nothing("No runs recorded yet.")
            return []
        if name:
            record = self.registry.get(name)
            if record is None:
                display.problem(f"No run named '{name}'.",
                                "/history on its own lists every record.")
                return None
            display.history_detail(record)
            return [record]
        display.history(records)
        return records

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

        Shared by _drive() and diagnose() so the transcript looks the same regardless
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

        Timed as one turn in telemetry, from the first stream to the last --
        including every question answered in place -- because that whole span
        is what a person waits through between typing a line and seeing a
        reply.

        This is also where a turn BEGINS, which is why the gate's
        one-submission-per-turn flag is cleared here: every path into the graph
        -- run(), resume(), and the questions answered in between -- goes
        through this method exactly once per turn, and a flag left standing
        would silently disarm the gate for the rest of the session.
        """
        self._submitted_this_turn = False
        self._generated_this_turn = False
        self._gate_returns = 0
        self._capability_calls = 0
        # Which existing runs this turn has looked at. See _run_capability: a
        # proposal that follows a lookup is a rebuild of something, and owes a
        # declaration of what it changed. Cleared per turn so a run inspected an
        # hour ago does not make every later proposal look derived.
        self._runs_examined = set()
        self.telemetry.start_turn()
        try:
            for _ in range(self.MAX_QUESTIONS_PER_TURN):
                self._stream(payload, config, on_step)
                value = self._interrupt_value(config)
                if not (isinstance(value, dict) and value.get("kind") == "ask"):
                    return
                payload = Command(resume={"answer": self._answer(value)})
        finally:
            self.telemetry.end_turn()

    def run(self, prompt, thread_id, on_step=None, name=_UNSET):
        """Add one turn to a conversation and drive it to a stop.

        thread_id names the CONVERSATION, not the run. It is the checkpoint key
        LangGraph saves state under, and it persists across turns so that this
        message arrives after everything already said rather than in front of a
        blank agent. Runs get their own names, minted at the gate -- see
        _settle().

        `name` overrides that minting, and exists for one caller: /modify's
        "hold as a new launch", where the person has already typed the name and
        would not recognise a derived one. It is passed through as _settle's
        `held`, which is the existing way of saying "the caller already knows
        which run this is".

        The history is replayed explicitly because Biomni's AgentState declares
        `messages` with no reducer, making it last-value-wins: passing only the
        new message would REPLACE the conversation instead of extending it. The
        alternative -- redeclaring the state with add_messages -- would change
        the semantics of a channel that /diagnose deliberately relies on, so the
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
        return self._settle(thread_id, config, task=prompt, held=name)

    def abandon(self, name, reason=None):
        """/reject, which is now terminal. Retire a held run without submitting.

        Nothing reaches the scheduler, nothing is regenerated, and no model is
        called -- which is the whole difference from /modify, and the reason
        this is a registry write and not a graph resume.

        The conversation's interrupt is deliberately left standing. LangGraph
        has no "discard this pause" operation, and inventing one by streaming a
        fresh input would destroy the checkpoint rather than tidy it. The run is
        out of held(), so nothing offers it any more; the thread simply ends its
        life parked, which costs a row in a sqlite file and no correctness.
        """
        record = self.registry.get(name)
        if record is None:
            record = self.registry.held_for_thread(name)
            if record:
                name = record["name"]
        if record is None:
            display.problem(f"No run named '{name}'.", "/list shows what there is.")
            return None
        if record["status"] != runs_store.HELD:
            display.problem(f"'{name}' is not held — it is {record['status']}.",
                            "Only a run waiting at the gate can be abandoned.")
            return None
        self.registry.abandon(name, reason)
        display.abandoned(name, reason)
        return name

    def rename(self, name, wanted):
        """Give a held run a different name. No model, no regeneration.

        The one row at the gate that changes nothing about what would run. It is
        here rather than in the registry alone because the refusals belong to
        the interface: a submitted run's name is tied to a job list and to jobs
        already on the scheduler, and moving it would strand both.
        """
        record = self.registry.get(name)
        if record is None:
            display.problem(f"No run named '{name}'.", "/list shows what there is.")
            return None
        if record["status"] != runs_store.HELD:
            display.problem(f"'{name}' has already been submitted.",
                            "Its name is tied to a job list now.")
            return None
        settled = self.registry.rename(name, wanted)
        if settled is None:
            display.problem(f"Could not rename '{name}'.")
            return None
        display.renamed(name, settled)
        if settled != wanted:
            display.nothing(f"'{wanted}' was taken, so it is '{settled}'.")
        return settled

    def scan(self, root, chosen=None):
        """Adopt GenPipes runs that already exist on disk. Read-only discovery.

        The discovery is deterministic local code -- runs.discover() -- and not
        a model wandering a filesystem. It reads job-list filenames, generated
        command names and directory names; it never opens a FASTQ, a BAM, a VCF
        or a readset. Nothing found is modified, renamed, regenerated,
        resubmitted or cancelled.

        `chosen` is the subset the user picked. Nothing is adopted without it:
        a scan that silently registered everything it found would be unusable
        the second time you ran it, and would put runs in /list that nobody
        asked to be responsible for.
        """
        root = os.path.abspath(os.path.expanduser(root or "."))
        found = runs_store.discover(root)
        if not found:
            display.scan_results(root, found)
            return []
        if chosen is None:
            display.scan_found(root, found)
            return found

        wanted = {str(c) for c in chosen}
        added, restored, skipped = [], [], []
        for entry in found:
            if entry["name"] not in wanted and entry["job_list"] not in wanted:
                continue
            # THREE OUTCOMES, NOT TWO. This used to be adopt-or-refuse, and
            # the refusal was a dead end: "already known as hi-0724" for a run
            # that was hidden, or gone, or not in fact the same run at all.
            # runs.rediscovery() decides which of the three this is; nothing
            # here re-derives it, so /scan and any other caller cannot disagree
            # about what finding a run again means.
            verdict = runs_store.rediscovery(self.registry, entry)
            if verdict.action == runs_store.KNOWN:
                skipped.append((entry["name"],
                                f"{verdict.reason} — on record as "
                                f"{verdict.name}"))
                continue
            if verdict.action == runs_store.RESTORE:
                self.registry.rediscover(verdict.name, entry)
                restored.append((verdict.name, verdict.reason))
                continue
            name = self.registry.unique_name(entry["name"])
            if name != entry["name"]:
                skipped.append((entry["name"], f"name taken — added as {name}"))
            self.registry.adopt(name, entry)
            added.append(name)
        display.scan_results(root, found, added=added, skipped=skipped,
                             restored=restored)
        return added + [n for n, _ in restored]

    def step_help(self, pipeline, protocol=None):
        """`genpipes <pipeline> [-t <protocol>] --help`, as text.

        The only authority on what a protocol's steps are and what they do.
        There is no step table in this repo and there must not be one:
        genpipes.md says so outright, because the numbered list is version-exact
        and a copy here would be wrong on the next GenPipes release while
        looking authoritative.

        Returns "" when it cannot be reached -- on a laptop, or with no module
        system -- and the caller must treat that as "no opinion", never as "no
        problem".

        A thin delegate now. The fetch moved to runs.pipeline_help so that this
        and gate's flag-surface lookup share one cache: they ask the same
        install the same question, and running the subprocess twice under a
        keystroke was the only difference between them.
        """
        return runs_store.pipeline_help(pipeline, protocol)

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
        # A GATE interrupt, not merely an interrupt. This used to accept any
        # pause on the thread, which meant a run parked on a QUESTION -- an
        # ask() the conversation never finished answering -- satisfied the one
        # check standing between /approve and _perform_submission. The two
        # pauses are the same LangGraph mechanism and only the payload tells
        # them apart, so the payload is what gets checked.
        pending = self.gate_interrupt(config)
        if pending is None or pending is runs_store.UNKNOWN:
            if record is None:
                display.problem(f"No run named '{name}'.",
                                "/list shows what there is.")
                return {"status": "unknown", "thread_id": None}
            # Nothing is parked here, so nothing can be approved. Said plainly,
            # because the caller used to be handed a bare "done" from the
            # checkpoint and printed "<name> · submitted" on the strength of
            # it -- for a run whose command had never been resumed. The status
            # carries `submitted: False` so no caller has to infer it again.
            status = self._gate_status(config)
            status["submitted"] = False
            status["record_status"] = record.get("status")
            if approved:
                # Reconciled BEFORE the refusal is worded, because the honest
                # explanation is not "there is no gate" -- that is a symptom.
                # It is whatever the evidence says actually happened, and for
                # the run this check exists to protect (46 jobs submitted, the
                # recording turn killed by an API error) the truthful answer
                # is "it already ran, here is where it stands".
                standing = self.standing_of(record)
                if standing.status != record.get("status"):
                    self.reconcile_registry([record])
                    record = self.registry.get(name) or record
                    status["record_status"] = record.get("status")
                if record.get("status") in runs_store.BEFORE_APPROVAL:
                    display.problem(
                        f"'{name}' is not waiting at the gate any more.",
                        f"{standing.why.capitalize()} — /modify {name} to "
                        f"rebuild it, or /reject to drop it.")
                elif record.get("status") in runs_store.AFTER_APPROVAL:
                    display.problem(
                        f"'{name}' has already been through /approve — it is "
                        f"{record.get('status')}.",
                        f"/check {name} for where it stands. Nothing was "
                        f"submitted a second time.")
            return status
        if approved:
            # AN APPROVAL AUTHORISES ONE EXACT PROPOSAL.
            #
            # The interrupt says which command was on screen when the decision
            # was offered; the record says which command _perform_submission
            # is about to run. Nothing compared them, and the whole "one
            # proposal" property rests on somebody doing so -- a box drawn for
            # steps 1-5 must not be able to launch a script built for 3-6,
            # whatever sequence of /modify and re-gate got the two out of step.
            #
            # Both sides must be present. Falsy is no opinion, exactly as it is
            # in usage.py: every proposal built before revisions existed
            # carries none, and none of them may become unapprovable for it.
            shown = pending.get("revision")
            current = (record or {}).get("revision")
            if shown and current and shown != current:
                display.problem(
                    f"Not approved -- '{name}' has changed since that box was "
                    f"drawn.",
                    f"The approval box described revision {shown}; the run now "
                    f"holds {current}. Nothing has reached the scheduler.  ·  "
                    f"/view {name} to see what it is now, then /approve it.")
                status = self._gate_status(config)
                status["submitted"] = False
                status["revision_mismatch"] = (shown, current)
                return status

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
            # The command itself may be incomplete even when the environment is
            # sound -- a readset the model never asked for, a design a protocol
            # needs but the conversation never supplied. gate.build_proposal
            # computed this against slots.gaps() when the proposal was built;
            # read straight off the stored record rather than the box on screen,
            # which may be a day old and could in principle have been edited
            # underneath it.
            incomplete = ((record or {}).get("proposal") or {}).get("missing")
            if incomplete:
                display.problem(
                    f"Not approved -- missing: {', '.join(incomplete)}.",
                    f"/modify {name} to add it, or /reject to abandon this run.")
                status = self._gate_status(config)
                status["missing"] = incomplete
                return status
            # A CHANGE THAT WAS ASKED FOR AND DID NOT LAND.
            #
            # The run this exists for: "rerun it, without override_walltime.ini",
            # a regenerated command that still carried the ini, and a submission
            # that went through because nothing between the sentence and the
            # scheduler had been given the means to notice. The gate now draws
            # that row red -- see modify.realized -- and drawing it is not
            # enough on its own, because the box may have been read quickly and
            # the whole point is that the person believes the change was made.
            #
            # REFUSED ONCE, NOT FOREVER. Submitting the command as it stands is
            # a legitimate thing to want: the model may have been right to
            # decline the change, or the person may have changed their mind. So
            # this states what did not happen and stops, and a second /approve
            # goes through -- approval is still typed, and now typed by somebody
            # who has been told the thing they would otherwise have missed. The
            # acknowledgement is written to the record rather than held in
            # memory, so closing the session does not silently clear it.
            #
            # Cleared by anything that produces a new proposal: _settle writes
            # `changed` fresh on every pass through the gate, and the
            # acknowledgement is stamped with the revision it was given for.
            # `changed` is a MAPPING when _settle wrote it and a LIST of row
            # names when cli._redraw did -- a change that cost no model call
            # (re-tuning a step already on -c) redraws the gate without
            # re-deriving verdicts. A list carries no verdicts, so there is
            # nothing in it to refuse; .items() on one was an AttributeError
            # raised before every other check in this function, including the
            # script-exists one.
            marks = (record or {}).get("changed")
            ignored = ([row for row, verdict in marks.items()
                        if verdict == modify.IGNORED]
                       if isinstance(marks, dict) else [])
            undeclared = (record or {}).get("undeclared")
            # ACKNOWLEDGED PER GATE PASS, not per revision.
            #
            # This was keyed on the proposal's revision and had two holes, both
            # of which disarmed the refusal in exactly the situation it exists
            # for. A revision is None for a proposal with no generated command,
            # and `None != None` is false, so the check never fired on one at
            # all. Worse, a revision is a hash of the command -- and the failure
            # being caught is "the model regenerated and the command did not
            # change", which produces the SAME hash. So after one acknowledged
            # refusal, asking for the change again and having it ignored again
            # reused the revision, matched the acknowledgement, and submitted
            # silently.
            #
            # A flag cleared by _settle on every pass through the gate has
            # neither problem: it is set only by a refusal, and any new
            # proposal -- identical command or not -- clears it, so each pass
            # gets its own refusal and its own deliberate second /approve.
            if (ignored or undeclared) and not (record or {}).get(
                    "acknowledged_ignored"):
                self.registry.update(name, acknowledged_ignored=True)
                if ignored:
                    headline = (f"Not approved -- {', '.join(sorted(ignored))} "
                                f"{'was' if len(ignored) == 1 else 'were'} "
                                f"asked for and the regenerated command does "
                                f"not reflect "
                                f"{'it' if len(ignored) == 1 else 'them'}.")
                else:
                    headline = f"Not approved -- {undeclared}."
                display.problem(
                    headline,
                    f"/view {name} to read what it actually says, or "
                    f"/modify {name} to change it. Nothing has reached the "
                    f"scheduler. If you meant to submit it as it stands, "
                    f"/approve {name} again.")
                status = self._gate_status(config)
                status["submitted"] = False
                status["ignored"] = ignored
                status["undeclared"] = undeclared
                return status

            # THE SCRIPT THE BOX SAYS IT WILL RUN HAS TO BE THERE.
            #
            # `bash <script>` on a path that does not exist cannot do anything
            # but fail, and it fails in the most expensive way this interface
            # has: the person spends the one irreversible keystroke, the graph
            # runs, the shell says `No such file or directory`, and what comes
            # back is an amber notice about a submission that never had a
            # chance. The 0812 ampliconseq run failed exactly here -- generated
            # as `-g ampliconseq_cit_cmd.sh`, proposed as `bash
            # ampliconseq_cit_test/ampliconseq_cit_cmd.sh`, two paths that were
            # never the same file.
            #
            # _approved_script() already resolves the proposal's script against
            # the run's workdir, the current directory and its output dir, and
            # already returns None for "it is in none of them". That None was
            # being read only as "no declared total to reconcile against"; it
            # is also, and more usefully, a submission that is going to fail.
            #
            # Only when the proposal names a script at all. A chunk_genpipes /
            # submit_genpipes pair names none, and refusing those would be a
            # guess dressed up as a check.
            declared = ((record or {}).get("proposal") or {}).get("script")
            if (declared and not self._approved_script(record)
                    and runs_store.resolvable(declared)
                    and not ((record or {}).get("proposal") or {}).get("generated")):
                display.problem(
                    f"Not approved -- {declared} is not on disk.",
                    f"There is no generation command on record to rebuild it "
                    f"from, so there is nothing to submit. Nothing has reached "
                    f"the scheduler.  ·  /reject {name}, or describe the run "
                    f"again.")
                status = self._gate_status(config)
                status["missing_script"] = declared
                status["submitted"] = False
                return status
        self.log = []

        if approved:
            return self._approve(name, record, thread_id, config, started,
                                 on_step)

        command = Command(resume={"approved": False, "feedback": feedback,
                                  "name": name})
        self._drive(command, config, on_step)
        # `held=name`, not None. `held` does not mean "is held", it means "the
        # caller already knows which run this is", and resume() always does.
        # Passing None let _run_name fall through to minting a fresh name off
        # the pipeline, which is how one approval produced a phantom second run
        # holding the same proposal on the same thread.
        status = self._settle(thread_id, config, held=name)

        # A run that was NOT approved must still be waiting when this returns.
        # The turn just went back through generate, and the model was free to
        # answer in prose -- which is exactly what should happen when the
        # feedback was a question rather than a change ("why stringtie?"). But a
        # turn that ends in <solution> ends the graph, so _settle sees "done",
        # holds nothing, and the run drops off /list: a question asked at the
        # gate would have silently destroyed the decision it was asking about.
        #
        # The prompt tells the model to re-propose after answering, and when it
        # does, _settle above has already held the run at a NEW gate and there
        # is nothing to do here. This is the floor under that instruction, not
        # a substitute for it.
        if not approved and (status or {}).get("status") != "paused":
            # Re-read: the turn just ran, and if the model re-proposed, the
            # record now holds a newer proposal than the one this call started
            # with. Re-gating the stale one would reinstate a command the
            # conversation has already moved past.
            fresh = self.registry.get(name) or record
            if self.regate(name, thread_id, fresh):
                # RECOMPUTED, because the status above was read before the
                # gate came back and would report the hole this call just
                # filled. Callers act on what is returned -- cli._cmd_approve
                # reads it, and so does every test of this path -- so handing
                # back "done" for a conversation that is demonstrably parked
                # at an interrupt would be the same class of lie as the status
                # word this whole change is about.
                status = self._gate_status(config)
                status["name"] = name
            else:
                self._lapse(name, "the decision was not left open and could "
                                  "not be restored")
        return status

    def _approve(self, name, record, thread_id, config, started, on_step=None):
        """What /approve does: build the script, then launch it. No model call
        anywhere between the person saying yes and the jobs being on Slurm.

        THE SCRIPT IS REBUILT, NOT ASSUMED. The box describes a genpipes
        GENERATION -- pipeline, protocol, -c, -r, -d, -o -- and what used to run
        on approval was `bash cmd.sh`, a FILE. Nothing bound the two together:
        gate.generation_command() parses the box out of the newest matching
        block in the transcript, while the script on disk was written by
        whatever happened to run last. Usually the same thing. Not necessarily.
        Somebody read a description of one command and approved the execution of
        a file that something else may have produced.

        Regenerating from the recorded command closes that, and takes a whole
        family of failures with it: a script that was never generated, one
        generated into a different directory, a `-g` path and a `bash` path that
        disagree, a stale script left over from an earlier attempt. None of them
        are reachable when the thing that runs is built, at this moment, from
        the text that was on screen.

        AND NO MODEL IS INVOLVED. This used to resume the graph and let the
        model's own <execute> block do the submitting, which put an inference
        engine inside the one span in this product that cannot be undone -- and
        it is how a single approval turned into a second approval box. Both
        commands here are read off the record and run as written. The model is
        told what happened afterwards and does not speak: the outcome is
        reconciled from the job list and the exit code, and display.post_approve
        prints it. See submission_gate's `done` branch for why a narrating turn
        was removed rather than instructed better.

        The reconciliation stays in a `finally` for the reason it always has:
        an exception in the REPORTING of a submission must never lose the
        submission.
        """
        script = baseline = None
        observation = None
        try:
            script, baseline, observation = self._perform_submission(
                name, record, started, on_step)
        finally:
            if observation is not None:
                self._reconcile_submission(name, started, script, baseline,
                                           config, observation=observation)
        if observation is None:
            # Refused before anything ran -- the regeneration failed, or the
            # script it was supposed to produce is not there. _perform_submission
            # has said which. The run is untouched and still waiting, so the
            # gate stands and the person can modify it or drop it.
            status = self._gate_status(config)
            status["submitted"] = False
            return status

        # THE DECISION IS RECORDED BEFORE THE GRAPH IS TOLD ABOUT IT, and in
        # that order for a reason: the record is the authority and the model's
        # copy is a rendering of it. Written after the reconciliation above, so
        # the outcome it carries is the graded one rather than a guess made
        # while the command was still running.
        settled = self.registry.get(name) or record or {}
        decision = self.registry.add_decision(
            name, "approved",
            revision=(settled.get("proposal") or {}).get("revision")
                     or settled.get("revision"),
            outcome=runs_store.Outcome(
                settled.get("status") or runs_store.SUBMIT_UNKNOWN,
                jobs_seen=settled.get("jobs_seen"),
                expected=settled.get("expected_jobs"),
                job_list=settled.get("job_list"),
                detail=settled.get("outcome_detail") or ""))

        # The graph is still parked on the interrupt this approval answered, and
        # it has to be let go or the conversation can never take another turn.
        # It resumes with the DECISION rather than with an instruction: nothing
        # is left to execute, so the gate node renders what happened and ends
        # the turn without a model call. See submission_gate's `done` branch.
        self._drive(Command(resume={"approved": True, "done": True,
                                    "decision": decision, "name": name}),
                    config, on_step)
        return self._settle(thread_id, config, held=name)

    def _perform_submission(self, name, record, started, on_step=None):
        """Generate the script and run it. Returns (script, baseline, observation).

        An observation of None means NOTHING WAS SUBMITTED and the caller must
        not reconcile: the record is left exactly as it was, still held, rather
        than being moved to `submitting` for a command that never ran.

        Both commands run in the run's own workdir. That is not a detail: every
        relative `-o`, `-g` and `-c` on the command line means a different file
        from a different directory, and a held run is explicitly designed to be
        approved from a session that started somewhere else, days later.
        """
        proposal = (record or {}).get("proposal") or {}
        workdir = (record or {}).get("workdir") or os.getcwd()
        if not os.path.isdir(workdir):
            workdir = os.getcwd()

        generation = proposal.get("generated")
        if generation:
            self._narrating(on_step, generation, "GENERATE")
            out, code = runs_store.run_block(generation, cwd=workdir)
            if code != 0:
                display.problem(
                    f"Not submitted -- the command could not be generated.",
                    f"GenPipes exited {code} rebuilding the script. Nothing has "
                    f"reached the scheduler.  ·  /modify {name}, or /reject it")
                display.output(out)
                return None, None, None

        script = self._approved_script(record)
        declared = proposal.get("script")
        if not script and runs_store.resolvable(declared):
            display.problem(
                f"Not submitted -- {declared} is still not there.",
                f"The generation reported success but did not write the script "
                f"the submission names. Nothing has reached the scheduler.  ·  "
                f"/modify {name}, or /reject it")
            return None, None, None

        # The baseline is taken BEFORE the command runs, because the only honest
        # measure of what this approval submitted is the number of job rows it
        # ADDED: GenPipes appends with >>, an output directory is routinely
        # reused, and a retry writes into the very same list as the attempt that
        # failed. Persisted to the record as well as held here -- the `finally`
        # in the caller covers an exception, it does not cover a kill or a
        # closed terminal, and the baseline is the one piece of evidence that
        # cannot be recovered afterwards.
        baseline = runs_store.job_list_state(
            runs_store.declared_job_list(script))
        self.registry.begin_submission(name, workdir=workdir,
                                       baseline=baseline, script=script,
                                       since=started)

        command = proposal.get("command") or ""
        self._narrating(on_step, command, "SUBMIT")
        out, code = runs_store.run_block(command, cwd=workdir)
        return script, baseline, runs_store.observation(out, code)

    def _narrating(self, on_step, code, _label):
        """Keep the spinner honest while a command runs outside the graph.

        on_step is the same callback the graph's own messages drive, and it
        classifies what it is handed -- so what it is handed is a message of the
        shape the classifier already reads, rather than a second vocabulary of
        labels that would drift from the first.
        """
        if not on_step:
            return
        try:
            on_step(AIMessage(content=f"<execute>\n#!BASH\n{code}\n</execute>"))
        except Exception:                       # noqa: BLE001
            pass                                # a spinner label is never worth a turn

    @staticmethod
    def prepared_transcript(request, generated):
        """The conversation behind a run the program built for itself.

        A revision produced without a model has no history, and an empty
        history is not neutral: /modify on that run would later send the model
        a change to make with no command in front of it to make it to, and
        gate.generation_command() -- which several checks read -- would find
        nothing at all.

        So the thread is opened with what actually happened, in one turn from
        the application's side: what was asked for, and the command that
        answers it. The command travels inside <execute> because that is the
        shape every reader in this file already knows how to find a generation
        in, and because it IS the block /approve will run.

        NOTHING IS ATTRIBUTED TO THE MODEL. There is no AIMessage here and
        there must not be: the model said nothing, and a transcript that
        claimed otherwise would be the same class of lie as a status word that
        says held for a run with no decision open. Same rule as
        _render_decision.
        """
        return [HumanMessage(content=(
            f"{request}\n\nThis run was prepared without you: every field of "
            f"it was already decided, so the command below was written "
            f"directly and is what the gate is holding.\n\n"
            f"<execute>\n{generated}\n</execute>"))]

    def _raise_gate(self, config, proposal, seed=None, on_error=None):
        """Open a real gate interrupt for `proposal`, with NO model call.

        The one mechanism two very different callers need. regate() uses it to
        put a decision back after a turn consumed it; hold_prepared() uses it to
        gate a proposal the program wrote itself. Both want the same thing --
        the actual LangGraph interrupt, carrying the actual proposal, so that
        /approve, /reject, /modify and the checkpoint all behave exactly as they
        do for a run the model built. A status word saying "held" is not that,
        and the difference between the two is the whole of regate's docstring.

        HOW IT AVOIDS THE MODEL. update_state(as_node="generate") writes the
        channels and evaluates that node's outgoing edge; routing_function sees
        `regate` and queues the gate; invoke(None) runs it. `generate` is never
        re-entered -- verified against the pinned LangGraph 0.3.18 rather than
        assumed, because this is the one mechanism in the file whose semantics
        are not obvious from its name.

        `seed` is a message list for a thread that has none -- a revision this
        program generated has no conversation behind it, and /modify on it later
        would otherwise send the model a change with no command in front of it.
        Written to the same `messages` channel a real turn writes, because it is
        the same fact: this is the command that exists. Nothing in it is
        attributed to the model.
        """
        # THE ONE-SUBMISSION-PER-TURN FLAG HAS TO BE CLEARED HERE.
        #
        # submission_gate()'s first branch ends the turn without interrupting
        # when `_submitted_this_turn` is set, which is right for a model that
        # re-proposes after a failed submission and wrong for this: the flag is
        # cleared by _drive(), and neither of these callers goes through it --
        # they invoke the graph directly. So after any approval in this
        # session, raising a gate silently produced no interrupt at all. What
        # that looked like: a revision written to the registry as `held` with
        # no decision behind it, and no message on screen either way, which is
        # exactly the "held is a status word rather than a decision" failure
        # regate() exists to have ended.
        #
        # Raising a gate IS the start of a turn -- something is about to be put
        # in front of a person -- so the flag is cleared the same way _drive
        # clears it.
        self._submitted_this_turn = False
        try:
            state = {"pending_proposal": dict(proposal), "next_step": "regate"}
            if seed is not None:
                state["messages"] = list(seed)
            self.app.update_state(config, state, as_node="generate")
            self.app.invoke(None, config)
        except Exception as e:                  # noqa: BLE001
            if on_error:
                on_error(e)
            return False
        if self.gate_interrupt(config) is None:
            # The graph ran and parked nothing. Reported through the same
            # handler an exception is, because it is the same outcome for the
            # caller and the alternative -- returning False in silence -- is
            # how a revision reached the registry with no decision behind it
            # and nothing on screen saying so.
            if on_error:
                on_error(RuntimeError("the graph parked no decision"))
            return False
        return True

    def hold_prepared(self, name, generated, script, workdir, thread_id,
                      declared=(), changed=(), warnings=(), seed=None):
        """Gate a proposal this program built. Returns the status, or None.

        THE PATH WITH NO INFERENCE IN IT. /relaunch knows every field of the
        revision it wants before anything runs, so there is nothing for a model
        to decide -- see relaunch.command(). What is left is the work a
        submission still needs: run the generation, parse the command that ran,
        check it against what was asked for, and stop at the gate.

        NOTHING IS WEAKENED BY THE MODEL'S ABSENCE. The proposal is built by
        gate.build_proposal, the same parse of the same command text; the
        install's own opinion is attached by the same with_usage; the
        declaration rides the same `_gate_note` the /modify panel uses, so
        modify.realized() reads the regenerated command back and the gate draws
        an IGNORED row exactly as it would have; and _settle does the holding,
        the verdicts and the box. The only thing that changed is who wrote the
        command.

        THE GENERATION REALLY RUNS, here rather than at /approve. It is not a
        model call -- it is `module load ... && genpipes ...` in a subprocess,
        the same command /approve will re-run -- and running it now is what
        makes the box honest: the script the submission names exists, GenPipes
        has accepted every flag, and a command it would have refused is refused
        HERE, before anybody spends an approval on it.

        The two refusals below are deliberately taken before the gate is raised
        rather than inside it. submission_gate() sends an incomplete proposal
        back to `generate` to be fixed -- which is right when a model is driving
        and is a model call this path exists to not make.
        """
        out, code = runs_store.run_block(generated, cwd=workdir)
        if code != 0:
            display.problem(
                f"'{name}' was not prepared -- the command could not be "
                f"generated.",
                f"GenPipes exited {code} building the script, so there is "
                f"nothing to approve. Nothing has reached the scheduler.")
            display.output(out)
            return None

        proposal = gate.build_proposal([], f'propose_submission("{script}")',
                                       generated=generated)
        proposal = gate.with_usage(proposal, runs_store.pipeline_usage(
            (proposal.get("slots") or {}).get("pipeline")))
        if declared:
            proposal["declared"] = list(declared)

        missing = proposal.get("missing") or []
        lacking = proposal.get("lacking") or []
        if missing or lacking:
            display.problem(
                f"'{name}' was not prepared -- the command it would build is "
                f"missing {', '.join(missing or lacking)}.",
                "The run it was copied from did not record them either. "
                "Nothing has reached the scheduler.")
            return None

        # NOTHING IS WRITTEN UNTIL THERE IS A DECISION TO WRITE. The registry
        # entry is made by _settle, after the interrupt exists -- so a graph
        # that parks nothing leaves no record claiming to be held, which is the
        # same rule regate() follows and for the same reason.
        self._gate_note = {"warnings": list(warnings or ()),
                           "changed": list(changed or ()),
                           "declared": list(declared or ())}
        config = self._config(thread_id)
        if not self._raise_gate(config, proposal, seed=seed,
                                on_error=lambda e: display.problem(
                                    f"'{name}' could not be put at the gate.",
                                    f"{type(e).__name__}: {e}  ·  nothing has "
                                    f"reached the scheduler.")):
            self._gate_note = {}
            return None
        status = self._settle(thread_id, config, held=name)
        # _settle records the run under os.getcwd(), which is right for a
        # command the model just ran in this process and wrong here: the
        # generation above ran in the source run's directory, and /approve
        # re-runs it from whatever the record says. The two have to be the same
        # directory or a relative -g resolves to two different files.
        if workdir and (self.registry.get(name) or {}).get("workdir") != workdir:
            self.registry.update(name, workdir=workdir)
        return status

    def regate(self, name, thread_id, record):
        """Restore the DECISION after a turn that consumed it without replacing it.

        WHAT THIS REPLACES, AND WHY THE OLD VERSION WAS WRONG.

        Everything typed at the gate that is not `/approve` resumes the graph
        with approved=False, so the model can read it. That is right: a
        question is a question, and answering it must not abandon the run. But
        resuming CONSUMES the interrupt, and if the model answers in prose
        without re-proposing, the turn ends with no decision open.

        The old code wrote `status = HELD` at that point. It did not restore
        the decision; it restored the appearance of one -- /list said "waiting
        for approval" and /approve said "not waiting at the gate any more",
        about the same run, on the same screen. Worse, it restored the last
        proposal WRITTEN TO THE RECORD, which after a rejection is the command
        the person rejected: ampliconseq-0804-2 was re-held pointing at an
        output directory its owner had just said no to.

        So this raises a real interrupt instead, for the proposal the record
        actually holds, and refuses outright in the cases where restoring one
        would be a lie.

        NO MODEL CALL. update_state(as_node="generate") writes the channel and
        evaluates that node's outgoing edge; routing_function sees `regate` and
        queues the gate; invoke(None) runs it. `generate` is never re-entered
        -- verified against the pinned LangGraph 0.3.18 rather than assumed,
        because this is the one mechanism in the file whose semantics are not
        obvious from its name.

        Returns True when a decision is now open.
        """
        proposal = (record or {}).get("proposal")
        if not proposal:
            return False

        # A REJECTED OR SUPERSEDED PROPOSAL IS NEVER RESURRECTED. This is the
        # barrier that makes ampliconseq-0804-2 structurally impossible: its
        # revision is on the rejected list the moment /reject records it, and
        # no amount of re-gating brings it back.
        revision = proposal.get("revision") or (record or {}).get("revision")
        if revision and revision in ((record or {}).get("rejected") or ()):
            return False
        if revision and revision in ((record or {}).get("superseded") or ()):
            return False

        config = self._config(thread_id)

        # HAS THE CONVERSATION MOVED PAST THIS PROPOSAL?
        #
        # The ampliconseq-0804-2 shape, and the one barrier that catches it.
        # Feedback at the gate is not always a question: "put it in a new
        # output directory" sends the model off to REGENERATE, and it may
        # succeed at that and then end the turn without proposing. The record
        # still holds the old proposal, it was never formally rejected, and
        # restoring it would put a command back in front of somebody who has
        # just finished asking for a different one.
        #
        # The history is consulted here for STALENESS ONLY -- never as the
        # thing that executes. What runs is always the durable proposal on the
        # record; this only asks whether that proposal is still the newest
        # thing the conversation produced. When it is not, there is nothing
        # honest to restore and the run lapses, saying so.
        newest = self._newest_revision(config)
        if newest and revision and newest != revision:
            display.problem(
                f"'{name}' was not put back at the gate.",
                f"A newer command was generated after that proposal and never "
                f"offered, so restoring the old one would show you something "
                f"the conversation has moved past.  ·  say 'propose it' to "
                f"gate the new one, or /modify {name}.")
            return False

        if not self._raise_gate(config, proposal, on_error=lambda e: display.problem(
                f"'{name}' could not be put back at the gate.",
                f"{type(e).__name__}: {e}  ·  /modify {name} rebuilds it.")):
            # A graph that cannot be re-parked is a run whose decision is
            # genuinely gone. The caller marks it lapsed, which is the truthful
            # outcome -- inventing a HELD status is the exact failure this
            # method exists to end.
            return False

        if self.gate_interrupt(config) is None:
            return False
        self.registry.hold(name, thread_id, proposal,
                           (record or {}).get("workdir") or os.getcwd())
        display.nothing(
            f"{name} is still waiting for a decision.",
            f"/approve {name}   ·   /modify {name} <change>   ·   /reject {name}")
        return True

    def _newest_revision(self, config):
        """The revision of the newest generation in this thread, or None.

        A STALENESS PROBE, and nothing else. It exists so regate() can tell
        "the model answered a question" from "the model went and built
        something different", which are the two ways a feedback turn can end
        and which want opposite treatment.

        Nothing executes from this. The proposal that runs is the one on the
        record; this only answers whether that proposal is still the newest
        command the conversation produced. Reading history for what to RUN is
        the thing that made _make_runnable dangerous; reading it to notice that
        a durable record has been overtaken is exactly what it is good for.

        None when there is no generation to compare against, which is
        no-opinion and lets the restore proceed.
        """
        try:
            snap = self.app.get_state(config)
        except Exception:                       # noqa: BLE001
            return None
        messages = list((snap.values or {}).get("messages") or [])
        generated = gate.generation_command(messages)
        if not generated:
            return None
        script = gate.flag_value(gate.invocation(generated) or generated, "-g")
        probe = gate.build_proposal(messages,
                                    f'propose_submission("{script}")'
                                    if script else "")
        return (probe or {}).get("revision")

    def _lapse(self, name, why):
        """Record that a run's decision is gone, and say what to do instead."""
        self.registry.update(name, status=runs_store.LAPSED,
                             reconciled_at=runs_store._now(),
                             reconciled_because=why)
        display.problem(
            f"'{name}' is no longer at the gate.",
            f"{why.capitalize()} — /modify {name} rebuilds it, or /reject to "
            f"drop it. Nothing was submitted.")

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
            # Read BEFORE the hold overwrites it. This is the only moment both
            # versions of the proposal exist in the same place, and the gate's
            # change marks are a comparison of the two -- see modify.compare.
            previous = (self.registry.get(name) or {}).get("proposal")
            self.registry.hold(name, thread_id, proposal, os.getcwd())
            if task:
                self.registry.update(name, task=task)
            blockers = self._blockers()
            status["blockers"] = blockers
            status["name"] = name
            # Risks carried over from the /modify that produced this proposal.
            # They belong in the box rather than in the modify flow's own output,
            # because the box is what a person reads at the moment of approving
            # -- a warning printed two screens earlier has already scrolled.
            # What the /modify that produced this proposal wants said about it.
            # Read and cleared in one move, whatever it held: a run that reaches
            # the gate by any other route has changed nothing since it was last
            # looked at, and a leftover note would claim it had.
            note = dict(getattr(self, "_gate_note", None) or {})
            self._gate_note = {}
            risks = list(note.get("warnings") or ())
            if risks:
                self.registry.update(name, warnings=risks)
            status["warnings"] = risks
            # WHAT ACTUALLY MOVED, not what was asked for.
            #
            # `previous` is read before registry.hold() overwrites it, because
            # after that there is nothing left to compare against. The diff is
            # over the proposals' SLOTS, which are a parse of the generated
            # command -- so this reports what the run will do, which is the
            # only question the gate is for.
            #
            # The old line took note["changed"] straight through: the rows
            # somebody ASKED to change, written before the model ran and never
            # checked. A change the model dropped came back green.
            requested = note.get("changed") or ()
            verdicts = modify.compare(previous, proposal, requested)
            # AND THEN WHAT THE COMMAND ITSELF SAYS.
            #
            # compare() diffs two proposals, which is the right question only
            # when there are two: a rerun or a fork lands under a new name and
            # has no earlier proposal to differ from, so it can say nothing --
            # and "nothing" is how a change somebody asked for in conversation
            # used to reach this box with no mark on it at all.
            #
            # A declaration is checkable without a baseline, because it is a
            # claim about the command that came back rather than about the
            # distance between two. Two sources, both structured, neither
            # parsed from anybody's prose:
            #
            #   note["declared"]        the /modify panel's own change set,
            #                           restated by modify.declaration
            #   proposal["declared"]    what the MODEL said it was changing,
            #                           from the changing() call in its
            #                           proposal block
            #
            # The panel's is applied second and wins where both speak, because
            # a row somebody selected by hand outranks a model's account of a
            # sentence about the same row.
            #
            # These OVERRIDE compare()'s answer for the rows they cover, and
            # that direction is the point: realized() read the resulting
            # command, compare() read a diff, and when they disagree the
            # command is the thing that will run.
            workdir = (self.registry.get(name) or {}).get("workdir")
            declared = proposal.get("declared")
            for source in (declared, note.get("declared")):
                if source:
                    verdicts.update(modify.realized(source, proposal, workdir))
            # A DERIVED RUN THAT NEVER SAID WHAT IT WAS CHANGING.
            #
            # The declaration is what makes realisation checkable, so a
            # modification that omits it is back in exactly the position this
            # whole mechanism exists to leave: a regenerated command with
            # nothing on screen to check it against. Enforced as SCHEMA
            # COMPLETENESS -- the model chose an action and the action is
            # incomplete -- and never by asking whether the user's sentence
            # sounded like a modification.
            #
            # `changes=[]` satisfies it. A deliberate rerun of exactly the same
            # command is a real thing to want, and saying so costs the model two
            # characters and leaves nothing ambiguous.
            #
            # Which proposals are DERIVED is decided from state and from the
            # model's own actions, never from prose:
            #
            #   previous is not None    this run name already had a proposal
            #   requested               a /modify or /fork panel produced it
            #   _runs_examined          the model looked an existing run up
            #                           through a capability this turn, which is
            #                           what "rerun Test_walltimefail" requires
            #                           it to do before it can rebuild anything
            # Recorded as a fact about the PROPOSAL rather than as a verdict on
            # a row, because it is not about any one row -- there is no row it
            # could be attached to without implying that row is the one at
            # risk. It becomes a warning in the box and a refusal at /approve.
            derived = bool(previous) or bool(requested) or bool(
                getattr(self, "_runs_examined", None))
            undeclared = ""
            if declared is gate.MALFORMED:
                undeclared = ("the change this run declares could not be read, "
                              "so nothing checked whether it was applied")
            elif derived and declared is None:
                undeclared = ("this rebuilds an existing run and did not "
                              "declare what it was changing, so nothing "
                              "checked whether your change was applied")
            if undeclared:
                risks = list(risks) + [undeclared]
                self.registry.update(name, warnings=risks)
                status["warnings"] = risks
            status["undeclared"] = undeclared
            # Cleared here, where a fresh proposal is recorded, so an
            # acknowledgement never outlives the box it was given for. See the
            # refusal in resume().
            self.registry.update(name, undeclared=undeclared,
                                 acknowledged_ignored=False)
            self.registry.update(name, changed=verdicts,
                                 requested=dict(requested)
                                 if isinstance(requested, dict) else
                                 list(requested))
            status["changed"] = verdicts
            status["requested"] = requested
            # Always drawn. There was a `quiet` flag here for /modify's "hold
            # for later", which applied a change and suppressed this box -- and
            # left the run in a state indistinguishable from the one this
            # produces. See cli._ask_ending for why it stopped being offered:
            # the batch case it was aimed at wants a flow of its own, not a
            # menu row whose only effect is skipping a repaint.
            # What was asked for, for the line under a row that did not move.
            # The declarations are the better source and are used where they
            # exist: `requested` is a list of row NAMES on every path that
            # writes it, so the isinstance test below has always been a
            # fallback for a shape nothing produces, and a red row with no
            # "you asked for ..." beneath it is the one this screen exists to
            # make legible.
            asked = dict(modify.wording(proposal.get("declared") or {}))
            asked.update(modify.wording(note.get("declared") or {}))
            if isinstance(requested, dict):
                asked.update(requested)
            display.gate(proposal, name, blockers=blockers, warnings=risks,
                         changed=verdicts,
                         wanted=asked or None,
                         resources=self._resource_summary(name))
        return status

    def _resource_summary(self, name):
        """One line describing this run's private override ini, or ''.

        Read off disk at draw time rather than carried on the record, because
        the file is the truth: /modify writes it, /diagnose will write it, and a
        person can edit it by hand between two glances at the gate. A cached
        summary would be the one thing on that screen that could be stale, on
        the screen whose entire job is to be current.
        """
        record = self.registry.get(name) or {}
        path = override.path_for(name, record.get("workdir") or os.getcwd(),
                                 record.get("proposal"))
        return override.summary(override.read(path))

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

    # ------------------------------------------------------------------- #
    #  Evidence, and the classification it supports.
    #
    #  The registry caches a status; this is what corrects it. Gathering lives
    #  here because it needs the graph, the filesystem and sometimes Slurm;
    #  DECIDING lives in runs.classify(), which is pure and stdlib-only, so
    #  the precedence rules are testable without any of that.
    # ------------------------------------------------------------------- #
    def gate_interrupt(self, config):
        """The GATE payload this thread is parked on, or None, or UNKNOWN.

        Three answers, and the third is why this is not a boolean.

          a proposal dict   a submission decision is genuinely open
          None              the graph is not parked on a gate. It may be
                            parked on an ASK, which is not a decision about a
                            submission and must never be mistaken for one --
                            resume() used to check only that SOME interrupt
                            existed, which would have let /approve spend an
                            approval into a question.
          runs.UNKNOWN      the checkpoint could not be read at all

        The last is the one that keeps a locked or missing database from
        retiring every pending decision in the registry.
        """
        try:
            snap = self.app.get_state(config)
        except Exception:                       # noqa: BLE001
            return runs_store.UNKNOWN
        if not (snap.next and snap.tasks and snap.tasks[0].interrupts):
            return None
        value = snap.tasks[0].interrupts[0].value
        if isinstance(value, dict) and value.get("kind") == "ask":
            return None
        return value if isinstance(value, dict) else None

    def evidence_for(self, record):
        """Everything durable that is known about one run, as plain data.

        Assembled in the order the sources can be trusted to survive: the
        filesystem first, because a job list outlives the conversation that
        made it; then the checkpoint, which outlives the process; then the
        graph's own pause. Slurm is not asked here -- it is the one source
        that can be unreachable for reasons having nothing to do with the run,
        and reconcile() already knows how to ask it when a verdict needs it.
        """
        evidence = {}

        # 1. Did this command run? Filesystem only -- see runs.ran_already for
        #    why the answer is True or UNKNOWN and never False.
        ran = runs_store.ran_already(record)

        # 2. The checkpoint may still hold what the submission printed, which
        #    is what turns "it ran" into a gradeable outcome. This is the
        #    evidence that resolves the 2026-07-29 case: the ids are in the
        #    observation even though the turn that would have recorded them
        #    died two nodes later.
        observation = None
        config = None
        ids = ()
        if record.get("thread_id"):
            config = self._config(record["thread_id"])
            observation = self._last_observation(config)
            # THE WHOLE THREAD, not just the last observation. A conversation
            # routinely carries on after a submission -- ampliconseq_demo
            # submitted 18 jobs and then spent four more turns watching them
            # with squeue -- so the newest observation is a status dump and the
            # launch is eight messages back. Reading only the last one found
            # nothing and filed a live run as a lapsed proposal.
            ids = self._submitted_ids_in(config)
        if ids:
            ran = True

        # SUBMITTING IS ALWAYS GRADED, evidence or none.
        #
        # It is the one status whose whole meaning is "we were in the middle of
        # this when we last looked", and leaving it standing is what it exists
        # to stop -- a record saying `submitting` about a session that ended
        # days ago. reconcile() already knows how to return SUBMIT_UNKNOWN when
        # it cannot establish anything, and an honest unknown is the point: the
        # status must move even when the evidence does not resolve it.
        #
        # Every other status is graded only on positive evidence, so a held
        # proposal is never dragged into a submission verdict by silence.
        if ran is True or record.get("status") == runs_store.SUBMITTING:
            baseline = record.get("job_list_baseline")

            # RECOVERY, when the run was never instrumented. No baseline and
            # no job list means this record predates begin_submission -- there
            # is nothing to difference, and the only durable account of what
            # happened is the ids the launch printed into the conversation.
            #
            # The script on disk is deliberately NOT consulted here, and that
            # is the correction that matters. A generated script is written to
            # a path a person reuses: `~/ampliconseq_cit_test/…_cmd.sh` was
            # rewritten by three later runs, and the file sitting there now
            # declares `TOTAL: 0 jobs`. Reconciling an August 4th submission
            # against it reported "no jobs — everything was already up to
            # date" about 18 jobs that genuinely ran. A stale artifact is not
            # evidence about a historical run; the transcript is.
            if (ids and not baseline and not record.get("job_list")
                    and record.get("status") != runs_store.SUBMITTING):
                evidence["outcome"] = runs_store.Outcome(
                    runs_store.SUBMITTED, jobs_seen=len(ids), expected=None,
                    detail=f"{len(ids)} jobs were submitted — recovered from "
                           f"the conversation, which is the only surviving "
                           f"account of this launch")
                evidence["submitted"] = True
                return evidence

            script = (record.get("submitted_script")
                      or self._approved_script(record))
            path = ((baseline or {}).get("path")
                    or runs_store.declared_job_list(script)
                    or record.get("job_list"))
            after = runs_store.job_list_state(path)
            outcome = runs_store.reconcile(
                script=script, observation=observation, baseline=baseline,
                after=after, quiet=None)
            # Only for a run that really was mid-flight, and only when the
            # first pass could not settle it. Asking the scheduler whether it
            # has been quiet since a given moment is what separates "the
            # submission failed" from "the submission failed after putting
            # work on the cluster", and it is the one question here that costs
            # a round trip -- so it is not asked about the twenty-odd held
            # proposals that never ran anything.
            if (outcome.status != runs_store.SUBMITTED
                    and record.get("status") == runs_store.SUBMITTING):
                outcome = runs_store.reconcile(
                    script=script, observation=observation, baseline=baseline,
                    after=after,
                    quiet=runs_store.scheduler_quiet_since(
                        record.get("submitted_since")
                        or _epoch(record.get("submitted_at"))))
            # A run whose only evidence is "ids were printed" still deserves a
            # graded outcome rather than a shrug: the count of ids IS the
            # number submitted, and with the job list long gone reconcile has
            # no other way to learn it.
            #
            # GRADED, not assumed. The ids are compared against the total the
            # script declared, exactly as rows_added would have been, so a
            # recovered outcome is held to the same standard as a live one. A
            # count that matches is submitted; anything else is unknown and
            # says both numbers, because "45 of 46" is a partial submission and
            # is precisely the thing nobody should be told was clean.
            if outcome.status != runs_store.SUBMITTED and ids and not outcome.jobs_seen:
                expected = outcome.expected
                matched = expected is not None and len(ids) == expected
                outcome = runs_store.Outcome(
                    runs_store.SUBMITTED if matched else runs_store.SUBMIT_UNKNOWN,
                    jobs_seen=len(ids), expected=expected,
                    job_list=outcome.job_list,
                    detail=(
                        f"{len(ids)} jobs were submitted — recovered from the "
                        f"conversation's own record of the launch"
                        if matched else
                        f"{len(ids)} jobs were submitted, but the script "
                        f"declared {expected if expected is not None else 'no'}"
                        f" total — recovered from the conversation, and it "
                        f"does not add up"))
            evidence["outcome"] = outcome
            evidence["submitted"] = True
            return evidence

        # 3. No submission. Is a decision actually open, and for what?
        if config is None:
            evidence["gate"] = False
            return evidence
        gate = self.gate_interrupt(config)
        if gate is runs_store.UNKNOWN:
            evidence["gate"] = runs_store.UNKNOWN
            return evidence
        evidence["gate"] = gate is not None
        if gate is not None:
            evidence["gate_revision"] = gate.get("revision")
        return evidence

    def standing_of(self, record):
        """What this record's evidence supports. Never writes."""
        try:
            return runs_store.classify(record, self.evidence_for(record))
        except Exception as e:                  # noqa: BLE001
            # A record we cannot reason about keeps the status it has. This
            # runs on every /list, and a malformed row must cost a row rather
            # than the listing.
            return runs_store.Standing(record.get("status"),
                                       f"could not be reconciled ({e})")

    def reconcile_registry(self, records=None, commit=True):
        """Bring every record's status into line with its evidence.

        The single reconciliation path. It replaces a version that visited only
        `submitting` records, which is how a run that submitted 46 jobs sat in
        /list offering approval for three weeks: it had never reached
        `submitting`, so the reconciler never looked at it, and the one status
        it was wearing was the one that claimed nothing had happened yet.

        Asking every non-terminal record the same question -- what does the
        evidence say? -- is what makes the CLASS detectable rather than the
        instance. A future crash in a node nobody has written yet leaves a
        record this pass can still resolve, because it does not need to know
        which node failed.

        `commit=False` reports without writing. That is not a debugging
        convenience: it is how a migration gets read before it is run, and the
        first thing to do with an unfamiliar registry.

        Returns [(record, Standing)] for everything whose status MOVED.
        """
        records = (self.registry.live(prune=False) if records is None
                   else list(records))
        moved = []
        for record in records:
            if record.get("status") in runs_store.TERMINAL:
                continue
            standing = self.standing_of(record)
            if not standing.status or standing.status == record.get("status"):
                continue
            moved.append((record, standing))
            if not commit:
                continue
            fields = {"status": standing.status,
                      "reconciled_at": runs_store._now(),
                      "reconciled_from": record.get("status"),
                      "reconciled_because": standing.why}
            if standing.outcome is not None:
                self.registry.record_outcome(record["name"], standing.outcome)
            self.registry.update(record["name"], **fields)
        return moved

    def reconcile_stale(self):
        """Called at startup. Reconciles the whole registry and says what moved.

        The name is kept because it is what the app calls; what it does is no
        longer limited to runs left mid-submission. See reconcile_registry.
        """
        moved = self.reconcile_registry()
        if moved:
            display.reconciled([(record["name"], standing.outcome)
                                for record, standing in moved])
        return [record["name"] for record, _ in moved]

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

        # A graph that DIED is not a graph that finished, and conflating them is
        # how /approve came to print "submitted" for a run it never touched. On
        # 2026-07-29 an API error killed the turn after the submission ran;
        # LangGraph wrote __error__ into the checkpoint, nothing read it, this
        # returned "done", and cli._cmd_approve took "done" as proof.
        #
        # snap.tasks[].error is where that marker surfaces. Reported as its own
        # status so no caller can mistake it for a completed turn.
        error = next((t.error for t in (snap.tasks or ()) if getattr(t, "error", None)),
                     None)
        if error is not None:
            return {"status": "errored",
                    "error": str(error),
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
        """The approval payload, with the install's own opinion attached.

        Two authorities, joined here because here is the only place both are
        reachable. gate.build_proposal is stdlib-only and runs in CI, so it
        cannot shell out to ask GenPipes anything; runs.pipeline_usage does
        exactly that and caches the answer. Keeping the parse pure and the
        lookup out here is what lets the gate's invariants keep running on a
        machine with no GenPipes on it.
        """
        proposal = gate.build_proposal(state.get("messages"), code)
        return gate.with_usage(proposal, runs_store.pipeline_usage(
            (proposal.get("slots") or {}).get("pipeline")))

    # --------------------------------------------------------------------- #
    #  Monitoring, in three sizes. check() is one aggregate number, jobs() is #
    #  every job, diagnose() is the only one that costs a model call.             #
    # --------------------------------------------------------------------- #
    def _approved_script(self, record):
        """The generated script this run's proposal submits, as an absolute path.

        Read off the proposal rather than guessed from the cwd, because the
        proposal is what was approved and the cwd is only where somebody was
        standing. Returns None when the script cannot be located, which
        reconcile() treats as "no declared total to check against" rather than
        as permission to assume success.

        The resolution itself is runs.resolve_path, which expands `~` and
        `$VARS` first. This function used to do the walk itself with a bare
        os.path.exists, so `-g ~/run/cmd.sh` -- a path bash expands without
        comment -- was looked for under a directory literally named `~`, never
        found, and reported as a script that did not exist. See runs.expand.
        """
        proposal = (record or {}).get("proposal") or {}
        return runs_store.resolve_path(
            proposal.get("script"),
            (record or {}).get("workdir"),
            os.getcwd(),
            (proposal.get("slots") or {}).get("output_dir"))

    def _last_observation(self, config):
        """What the execute node last returned on this thread, or None.

        Read back out of the checkpoint rather than captured in flight, so it
        survives the exception this whole path exists to survive. None means we
        never saw it, which reconcile() classifies as unknown -- never as a
        clean run.
        """
        try:
            snap = self.app.get_state(config)
        except Exception:                       # noqa: BLE001 -- see below
            # A checkpoint we cannot read is exactly the case that must not
            # raise: this runs in a `finally`, and an exception here would
            # replace the original failure with a confusing one and skip the
            # record write that is the entire point.
            return None
        for message in reversed(list((snap.values or {}).get("messages") or [])):
            content = str(getattr(message, "content", "") or "")
            if "<observation>" in content:
                return content
        return None

    def _submitted_ids_in(self, config):
        """Every distinct Slurm id this thread ever reported submitting.

        The whole history, oldest to newest, deduplicated. A submission is not
        always the last thing that happened on a thread -- the conversation
        usually carries on watching the jobs it just made -- so the launch has
        to be looked for rather than assumed to be at the end.

        Deduplicated because a thread can legitimately mention the same id
        twice (the launch, then a squeue dump quoting it back), and counting it
        twice would turn 18 jobs into 36.

        Returns a tuple, empty when nothing was found or the checkpoint could
        not be read. Empty means "no evidence", never "nothing was submitted".
        """
        try:
            snap = self.app.get_state(config)
        except Exception:                       # noqa: BLE001
            return ()
        found = {}
        for message in list((snap.values or {}).get("messages") or []):
            content = str(getattr(message, "content", "") or "")
            if "Submitted job with ID" not in content:
                continue
            for job_id in runs_store.submitted_ids(content):
                found.setdefault(job_id, None)
        return tuple(found)

    def _reconcile_submission(self, name, since, script, baseline, config,
                              observation=None):
        """Establish what the approved command actually did, and record it.

        Runs in a `finally`, so it happens whether the turn ended cleanly,
        raised, or was interrupted. Nothing here may raise: this is the last
        chance to write down that a submission occurred, and a traceback from
        the bookkeeping would lose exactly what the bookkeeping is for.

        No model is consulted. The four facts are the script's declared total,
        whether the runner reported a non-zero exit, how many job rows appeared
        that were not there before, and -- only when a failure needs grading --
        what Slurm says. See runs.reconcile for how they combine, and for why a
        clean exit alone is never enough.

        `observation` is what the submission printed, when the caller ran it
        itself and therefore has it in hand (_perform_submission does). Left
        None, it is read back out of the checkpoint instead -- which is where it
        lives when the graph did the running, and which is deliberately a READ
        rather than something captured in flight, so it survives the exception
        this whole path exists to survive.
        """
        try:
            record = self.registry.get(name) or {}
            proposal = record.get("proposal")
            if observation is None:
                observation = self._last_observation(config)

            after = runs_store.job_list_state((baseline or {}).get("path"))
            # No declared list to watch -- an older script, or one whose header
            # could not be read. Fall back to the mtime search, which is weaker
            # (it cannot produce a delta) but is better than no path at all.
            if not (baseline or {}).get("path"):
                slots = (proposal or {}).get("slots") or {}
                found = runs_store.find_job_list(
                    os.getcwd(), since, output_dir=slots.get("output_dir"),
                    script=(proposal or {}).get("script"))
                after = runs_store.job_list_state(found)
                baseline = None

            outcome = runs_store.reconcile(
                script=script, observation=observation,
                baseline=baseline, after=after, quiet=None)

            # Slurm is asked only when the answer changes what may be offered:
            # a failed or unestablished outcome, where the question is whether a
            # bare retry could double-submit. A successful one needs no query,
            # and querying on every approval would put an sacct call on the
            # happy path for nothing.
            if outcome.status in (runs_store.SUBMIT_FAILED,
                                  runs_store.SUBMIT_UNKNOWN):
                outcome = runs_store.reconcile(
                    script=script, observation=observation,
                    baseline=baseline, after=after,
                    quiet=runs_store.scheduler_quiet_since(since))

            self.registry.record_outcome(name, outcome, workdir=os.getcwd(),
                                         proposal=proposal)
            return outcome
        except Exception as e:                  # noqa: BLE001 -- see docstring
            try:
                self.registry.record_outcome(
                    name,
                    runs_store.Outcome(
                        runs_store.SUBMIT_UNKNOWN,
                        detail=f"the outcome could not be established ({e})"),
                    workdir=os.getcwd())
            except Exception:                   # noqa: BLE001
                pass
            return None

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
            # This used to have to guess between "no jobs were created" and
            # "the list was written somewhere we did not look", because nothing
            # had counted. reconcile() now has, so say what was established and
            # fall back to the old both-answers wording only for records that
            # predate it.
            seen, expected = record.get("jobs_seen"), record.get("expected_jobs")
            if runs_store.jobs_are_unreachable(record):
                # SAY WHAT IS KNOWN. This used to fall through to the guess
                # below -- "either every step was already up to date, or
                # GenPipes wrote its list outside the directories searched" --
                # and offer both possibilities about a run whose answer had
                # already been established: 46 jobs, recovered from the
                # conversation's own record of the launch. Neither branch of
                # the guess was true, and the one fact anybody had was left out
                # of it.
                #
                # What genuinely cannot be done is named separately, because it
                # is a different thing: without a manifest there are no job ids
                # to ask Slurm about, so per-job state is gone even though the
                # run's outcome is not.
                display.problem(
                    f"'{name}' submitted {seen} job{'s' if seen != 1 else ''}, "
                    f"but no job list was recorded for it.",
                    f"{record.get('outcome_detail') or ''}"
                    f"{'  ·  ' if record.get('outcome_detail') else ''}"
                    f"Without the manifest there are no job ids to ask the "
                    f"scheduler about, so this run's jobs cannot be inspected."
                    f"  ·  /track {name} <path/to/job_list> if you still have "
                    f"it  ·  /history {name} for what is recorded")
            elif record["status"] == runs_store.SUBMITTED and seen == 0:
                display.problem(
                    f"'{name}' created no jobs — every step was already up to "
                    f"date.", "There is nothing on the scheduler to check.")
            elif record["status"] in (runs_store.SUBMIT_FAILED,
                                      runs_store.SUBMIT_UNKNOWN,
                                      runs_store.SUBMITTING):
                display.problem(
                    f"'{name}' is {record['status'].replace('_', ' ')} — "
                    f"{record.get('outcome_detail') or 'no job list was recorded'}.",
                    "squeue -u $USER shows what is actually queued  ·  "
                    f"/track {name} <path/to/job_list> adopts a list by hand")
            else:
                display.problem(
                    f"No job list was recorded for '{name}' -- either every "
                    f"step was already up to date, or GenPipes wrote its list "
                    f"outside the directories searched."
                    + (f" {seen} of {expected} jobs were counted."
                       if seen is not None and expected is not None else ""),
                    f"/track {name} <path/to/job_list>")
            return None
        return record

    def check(self, name):
        """Show a run's progress, as the scheduler sees it.

        The cheap, deterministic answer to "how is it going" -- no model, no
        cost. What changed here is where the answer comes from: this used to
        report GenPipes' own `tools log_report`, which never contacts Slurm and
        infers state from files on disk. On a run that died at 10:12 it reported
        1 COMPLETED, 2 RUNNING, 43 PENDING; sacct said 1 COMPLETED, 2 TIMEOUT,
        43 CANCELLED. The run had been dead for hours and this command called it
        healthy and in progress. See the comment above runs.resolve() for why no
        amount of better file-reading could have fixed that.

        The result is cached on the record so /list can show where each run
        stood without a scheduler round-trip per row -- and the pending reasons
        are cached too, which is the one thing here that genuinely cannot be
        recovered later.
        """
        if str(name).lower() == "all":
            return self.check_all()
        record = self._need_run(name)
        if record is None:
            return None
        status = runs_store.resolve(record)
        if status.total:
            self.registry.remember_check(name, status.counts, status.total,
                                         status.verdict)
        self.registry.remember_reasons(name, status.reasons)
        display.run_status(name, status)
        return status

    def check_all(self):
        """/check all -- every live run, grouped by what it needs from you.

        One scheduler query, whatever the number of runs. Job ids are globally
        unique, so every manifest's ids go into a single sacct call and the
        results are attributed back by id. Looping check() would be one sacct
        and one squeue per run, and on a login node with a fortnight of
        experiments in the registry that is the difference between a listing and
        a wait.

        Grouped rather than listed, and that is the whole design of this view.
        The question it answers is "what should I be doing", and the answer to
        that is never chronological and never alphabetical: anything failed,
        blocked or held goes to the top whatever else is happening, and a run
        that finished cleanly is one line at the bottom saying so.

        There was briefly a second command for this -- /status all -- rendering
        the same query as a flat table, and /status <name> was an exact alias
        for check(). Two layouts of one query is one layout too many, so the
        grouped one won and the flat one went; the progress figure it carried
        moved into these rows.

        One run that cannot be checked must not cost the whole command. It gets
        a row reading unavailable and the rest are still resolved -- a monitor
        that fails entirely when one of twenty runs has a purged job list is a
        monitor you learn not to run.
        """
        records = self.registry.live()
        rows = runs_store.resolve_all(records)
        groups = {display.ACTIVE: [], display.ATTENTION: [], display.FINISHED: []}

        for record, status in rows:
            name = record["name"]
            slots_ = (record.get("proposal") or {}).get("slots") or {}
            what = " ".join(str(slots_[k]) for k in ("pipeline", "protocol")
                            if slots_.get(k)) or None
            when = (record.get("submitted_at") or record.get("held_at") or "")
            when = when.replace("T", " ")[5:16] or None

            if record.get("status") == runs_store.HELD:
                groups[display.ATTENTION].append({
                    "name": name, "what": what, "when": when,
                    "line": "held at the gate, nothing submitted",
                    "suggest": f"/approve {name}  ·  /modify {name}  ·  /reject {name}"})
                continue
            # A submission whose outcome is not a clean success outranks
            # everything else here, because it is the only row that may mean
            # work is on the cluster that nobody is tracking -- and the only one
            # where the wrong next action submits a pipeline twice.
            if record.get("status") in (runs_store.SUBMIT_FAILED,
                                        runs_store.SUBMIT_UNKNOWN,
                                        runs_store.SUBMITTING):
                seen = record.get("jobs_seen")
                line = record.get("status").replace("_", " ")
                if seen:
                    line += f" — {seen} job(s) recorded before it stopped"
                elif record.get("outcome_detail"):
                    line += f" — {record['outcome_detail']}"
                groups[display.ATTENTION].append({
                    "name": name, "what": what, "when": when, "line": line,
                    "suggest": (f"/check {name}" if record.get("retry_safe")
                                else f"/check {name}  ·  squeue -u $USER")})
                continue
            if status is None and not record.get("job_list"):
                # A submission where every step was already up to date creates
                # no jobs and writes no list. That is a real and successful
                # outcome, and filing it under NEEDS ATTENTION would send
                # somebody looking for a problem that does not exist.
                groups[display.FINISHED].append({
                    "name": name, "what": what, "when": when,
                    "line": "no jobs — everything was already up to date",
                    "suggest": None})
                continue
            if status is None or status.source == "unavailable":
                groups[display.ATTENTION].append({
                    "name": name, "what": what, "when": when,
                    "line": "status unavailable — the scheduler could not be reached",
                    "suggest": None})
                continue

            # Both the count and the percentage. The count is what you act on --
            # "1 of 46" is a different situation from "45 of 46" even though
            # both are unfinished -- and the percentage is what the eye reads
            # first. It came from the flat table this view replaced.
            done = (f"{status.counts.get('COMPLETED', 0)} of {status.total} done"
                    f"  ({status.percent:.0f}%)")
            broke = sum(n for s, n in status.counts.items()
                        if s in runs_store.BROKE_STATES)
            if broke or status.doomed or status.unknown:
                trouble = (f"{broke} failed" if broke else
                           f"{status.doomed} will never run" if status.doomed else
                           f"{status.unknown} unaccounted for")
                cause = status.root_cause
                if cause:
                    trouble += f" — {cause['step']}"
                groups[display.ATTENTION].append({
                    "name": name, "what": what, "when": when,
                    "line": f"{trouble}  ·  {done}",
                    "suggest": f"/diagnose {name}"})
            elif status.finished:
                groups[display.FINISHED].append({
                    "name": name, "what": what, "when": when,
                    "line": done, "suggest": None})
            else:
                live = status.counts.get("RUNNING", 0)
                queued = status.counts.get("PENDING", 0)
                groups[display.ACTIVE].append({
                    "name": name, "what": what, "when": when,
                    "line": f"{live} running, {queued} queued  ·  {done}",
                    "suggest": None})

        # NEEDS ATTENTION by urgency: something broken outranks something merely
        # waiting on a person, because the broken one is already costing time.
        groups[display.ATTENTION].sort(key=lambda r: "held" in r["line"])
        groups[display.ACTIVE].sort(key=lambda r: r.get("when") or "", reverse=True)
        groups[display.FINISHED].sort(key=lambda r: r.get("when") or "", reverse=True)

        for record, status in rows:
            if status is not None and status.total:
                self.registry.remember_check(record["name"], status.counts,
                                             status.total, status.verdict)
                self.registry.remember_reasons(record["name"], status.reasons)
        display.status_overview(groups)
        return groups

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

    def diagnose(self, name, question=None, on_step=None):
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

        # Resolved first, so the diagnosis is anchored on what the scheduler
        # says rather than on what triage happened to find logs for. It also
        # supplies the two facts triage cannot: the root cause ordered by start
        # time, and the pending reasons -- which sacct never records and squeue
        # forgets, so if they were not captured while the run was alive they are
        # gone. A run that is dead because 28 jobs are queued behind a
        # dependency that will never be satisfied has NO failed job to triage,
        # and the old refusal below would have sent that person away.
        # PHASES, RECORDED SEPARATELY, under the debug mode that already
        # exists (GENPIPE_TELEMETRY=1, read back with /telemetry). The question
        # this answers is where a 76-second diagnosis actually goes, and it is
        # worth being able to answer it without a stopwatch: the deterministic
        # half measured at ~40ms, so anything else is the model and its tools,
        # and the fix for that is grounding rather than optimisation.
        status = self.telemetry.timed(
            "diagnose.scheduler", runs_store.resolve, record)
        report = self.telemetry.timed(
            "diagnose.evidence", runs_store.triage, record, jobs=status.jobs)
        stored = (record.get("last_reasons") or {}).get("reasons") or {}
        reasons = status.reasons or stored

        if not report["failed_total"] and not status.doomed:
            display.problem(f"Nothing in '{name}' has failed.",
                            f"/check {name} for where it's up to.")
            return None

        if report["failed_total"]:
            display.triage(name, report)
        else:
            display.run_status(name, status)

        thread = f"{name}::why-{datetime.datetime.now():%m%d%H%M%S}"
        prompt = self.telemetry.timed(
            "diagnose.brief", self._diagnose_prompt, name, record, report,
            question, status=status, reasons=reasons)
        self.critic_count = 0
        self.user_task = prompt
        self.log = []
        display.defer_solution(True)
        try:
            self.telemetry.timed(
                "diagnose.model", self._drive,
                {"messages": [HumanMessage(content=prompt)],
                 "next_step": None}, self._config(thread), on_step)
        finally:
            display.defer_solution(False)
        # The RunStatus is about to be shadowed by the gate status, and its
        # tally is the half of the finding the parsed answer does not carry.
        # Kept here, at the last moment both exist, for the same reason
        # _settle reads `previous` before hold() overwrites it.
        scheduler = status
        status = self._gate_status(self._config(thread))

        # Drawn here rather than left to _drive's transcript renderer, because
        # the answer has a shape now and the transcript renderer only knows
        # prose. An answer that came back unshaped falls through to exactly the
        # prose rendering it always had -- see display.diagnosis.
        parsed = diagnosis.parse(status.get("final") or "")
        # Not when the investigation itself hit the gate. A read-only intent is
        # not a guarantee -- the graph is gated for exactly that reason -- and
        # if the model decided to resubmit, `final` is the pause, not an answer.
        if status.get("final") and status.get("status") != "paused":
            # WHETHER THE FIX IS ONE WE CAN APPLY is decided here, by
            # override.applicable(), and handed to the renderer as an answer.
            # The screen offers /relaunch on that verdict and on nothing else
            # -- not on "the run failed", and not on "there is an OVERRIDE
            # heading", which is a claim about the model's output rather than
            # about what this program can do with it.
            good, _ = override.applicable(parsed.get("override") or {})
            display.diagnosis(name, parsed,
                              logs=[f["log"] for f in report["findings"]
                                    if f.get("log")],
                              applicable=bool(good))
            # The one-line note keeps the CAUSE where there is one. /history six
            # weeks later wants "gatk_sam_to_fastq killed at its walltime", not
            # the first 140 characters of a heading.
            self.registry.add_note(
                name, _one_line(parsed["cause"] or status["final"]))
        # PARSED AND KEPT AS DATA, which is what makes /relaunch possible at
        # all. The override fragment is already structured by diagnosis.parse
        # -- section, key, value -- so nothing downstream has to read prose off
        # a screen to act on it. /modify's resources row offers it instead of
        # making somebody retype it; /relaunch applies it into a revision.
        #
        # THREE THINGS ARE STORED, NOT ONE. The fix on its own is a
        # recommendation with its reservations stripped off:
        #
        #   override   what to change
        #   uncertain  what this run does NOT establish about it -- including,
        #              routinely, whether the value proposed is sufficient.
        #              It survives to /relaunch's review screen, which is the
        #              last screen before somebody spends an allocation on it.
        #   before     what the run's own config trace recorded for the same
        #              keys, so the review can say `0:10:00 -> 35:00:00` from
        #              two observations rather than showing a value with no
        #              baseline. Read from the trace here, while the trace path
        #              is in hand -- it is a snapshot of a moment that is over.
        if parsed.get("override"):
            self.registry.remember_remediation(
                name, parsed["override"], parsed.get("uncertain") or (),
                before=_trace_values(report.get("trace"), parsed["override"]))
        status["diagnosis"] = parsed
        # WHAT THIS RUN'S DIAGNOSIS ESTABLISHED, as data, on the value the
        # caller gets back. The screen already has all of it; what did not have
        # it was the conversational model, which reaches diagnose_run through a
        # capability and was told only that a lookup had happened -- see
        # _capability_note, whose diagnose_run branch was unreachable because
        # this function returns a dict and the branch tested for a RunStatus.
        #
        # `name` is carried explicitly and is the name that was diagnosed. It is
        # the anchor every other value here is under: a note built from this
        # cannot attribute one run's tally to another, because there is only
        # ever one run in it.
        status["evidence"] = {
            "name": name,
            "verdict": scheduler.verdict,
            "counts": dict(scheduler.counts or {}),
            "total": scheduler.total,
            "root_cause": dict(scheduler.root_cause or {}),
        }
        return status

    def _diagnose_prompt(self, name, record, report, question, status=None,
                    reasons=None):
        """Build the diagnosis prompt out of facts, not prose.

        Every value here was parsed from a command or read from the scheduler, so
        the model is arguing from the same evidence the user can see on screen
        above it. The instruction to answer in a <solution> block matters: without
        it the agent's default is to start running code, and the first thing worth
        having is a hypothesis, not more shell.

        Three facts are stated that the log tails cannot supply, and each one
        exists to head off a specific wrong answer:

          the full state tally    so "three things failed" is not concluded from
                                  forty cancellations.
          the root cause          the EARLIEST independent failure. Without it a
                                  model reads the logs it was given in the order
                                  it was given them and names whichever it liked.
          the pending reasons     the only place a dependency that will never be
                                  satisfied is recorded. sacct never had it and
                                  squeue has already forgotten.
        """
        slots = (record.get("proposal") or {}).get("slots") or {}
        lines = [
            f"A GenPipes run named '{name}' needs diagnosing.",
            "",
            "What is known, gathered from the scheduler and the run's own files:",
            f"  working directory: {record.get('workdir') or 'unknown'}",
        ]
        if status is not None:
            tally = ", ".join(f"{n} {s}" for s, n in sorted(status.counts.items()))
            lines.append(f"  scheduler says: {tally} (of {status.total} submitted)")
            lines.append(f"  source: {status.source}")
            cause = status.root_cause
            if cause:
                # NAMED, not just counted. "gatk_sam_to_fastq, 1 job(s)
                # TIMEOUT" tells a model which STEP broke and leaves it to
                # work out which of that step's jobs did -- and on a paired
                # tumour/normal run there are two candidates with almost the
                # same name. On 2026-08-05 the model was handed exactly that
                # and answered "tumorPair_COLO829N (or T -- one of the two)",
                # leading with the sample that had COMPLETED in 00:01:39.
                # resolve() knew it was T all along, and /check printed it.
                lines.append(
                    f"  earliest independent failure: {cause['step']}, "
                    f"{cause['count']} job(s) {cause['state']}"
                    + (f", first of them {cause['job']}"
                       if cause.get("job") else "")
                    + (f", ran {cause['elapsed']} of {cause['timelimit']}"
                       if cause.get("timelimit") else ""))
                # WHOSE MEASUREMENT THAT IS, said before the log arrives with a
                # different one. A job's GenPipes epilogue times its own window
                # -- from the script's start to the script's end -- while sacct
                # times the allocation. For 18382352 those were 00:10:19 and
                # 00:10:22, four seconds apart at the start and one at the end,
                # and each is internally consistent. Feeding the log tail (which
                # is right, and new) put the second number in front of the model
                # for the first time, and it quoted that one as though it were
                # the scheduler's. Naming the source is what keeps two true
                # measurements from collapsing into one wrong citation.
                if cause.get("elapsed"):
                    lines.append(
                        "  That elapsed time is SACCT'S: it measures the "
                        "ALLOCATION, start to end, and it is the figure the "
                        "walltime was enforced against. A GenPipes epilogue in "
                        "the .o log reports a different quantity -- the job "
                        "SCRIPT'S own runtime, which begins after the "
                        "allocation does and ends when the script does, so it "
                        "is a measurement nested inside sacct's and will be "
                        "shorter by seconds. They are two windows, not two "
                        "readings of one. If you quote both, say which "
                        "measured which; never merge them into one number or "
                        "attribute one figure to both sources.")
                # COUNTED, NOT CHARACTERISED. cancelled_after is
                # sum(state == CANCELLED) over the whole run -- see
                # runs._root_cause. It is a count of jobs, not a proof that
                # each was cancelled BY this failure, and it says nothing
                # about steps: 13 jobs of this run completed before the
                # failure, so "every step after it was cancelled" is false
                # about the run even when the arithmetic happens to fit.
                # THE DEPENDENCY CLOSURE, not a bare count. "32 CANCELLED"
                # is a tally over the whole run and says nothing about whether
                # THIS job caused them. The manifest's dependency column is
                # the only record of the DAG -- sacct never had it, squeue has
                # forgotten it -- and the model went and read the raw job_list
                # with `head -40` to reconstruct exactly this.
                cancelled = cause.get("cancelled_after") or 0
                waiting = runs_store.downstream_of(status.jobs,
                                                   cause.get("job_id"))
                if waiting:
                    states = {}
                    for j in status.jobs or ():
                        if j.job_id in waiting:
                            states[j.state] = states.get(j.state, 0) + 1
                    tally = ", ".join(f"{n} {s}" for s, n in sorted(
                        states.items(), key=lambda kv: -kv[1]))
                    lines.append(
                        f"  {len(waiting)} job(s) waited on this one, directly "
                        f"or through a chain of dependencies, and they are: "
                        f"{tally}. That is the manifest's own dependency "
                        f"column -- you do not need to read the job list to "
                        f"establish it.")
                if cancelled:
                    lines.append(
                        f"  {cancelled} job(s) in this run are CANCELLED "
                        f"overall. A CANCELLED job never started, so it wrote "
                        f"no log and its log explains nothing. Say how many "
                        f"jobs were cancelled -- do not say that every later "
                        f"step was, and do not describe branches that "
                        f"completed as though they had not.")
        if reasons:
            lines.append("  jobs still queued, and what they are waiting on: "
                         + ", ".join(f"{n} {why}" for why, n in
                                     sorted(reasons.items(), key=lambda kv: -kv[1])))
            if reasons.get(runs_store.DOOMED_REASON):
                lines.append("  DependencyNeverSatisfied means those jobs will "
                             "NEVER run, whatever sacct calls them.")
        for label, key in (("pipeline", "pipeline"), ("protocol", "protocol"),
                           ("readset", "readset"), ("pairs", "pairs"),
                           ("design", "design")):
            if slots.get(key):
                lines.append(f"  {label}: {slots[key]}")
        # THE COMMAND ITSELF. The model opened its last diagnosis by calling
        # show_run() to find out what this run WAS -- a whole round trip for a
        # string sitting on the record. Naming it here is not duplication: the
        # brief is what the model reads, and a fact it has to ask for is a fact
        # it was not given.
        command = (record.get("proposal") or {}).get("command")
        if command:
            lines.append(f"  the command that produced it: {command}")
        if slots.get("inis"):
            lines.append(f"  config layering: {' , '.join(slots['inis'])}")
        if record.get("job_list"):
            lines.append(f"  job list: {record['job_list']}")

        # THE STEP RANGE, ESTABLISHED HERE RATHER THAN LOOKED UP THERE.
        #
        # `-s` has no argparse default, so a run submitted without one carries
        # `steps: None` -- and this block used to print the row only when it
        # was truthy, which meant the commonest case told the model NOTHING and
        # left it to work out both what was originally asked for and what the
        # protocol's full range is. It did that by shelling out to
        # `module load genpipes && genpipes dnaseq --help` and reading the
        # printed list: 1.6s, a model round trip, and a constant per GenPipes
        # version rediscovered on every diagnosis.
        #
        # Both halves are now stated. The range comes from
        # genpipes_facts.json, generated from a real install by
        # tools/genpipes_facts.py -- the same source --help prints from, one
        # layer earlier. When the manifest does not record it, step_range()
        # returns None and this says nothing rather than asserting a range.
        asked = slots.get("steps")
        lines.append(f"  steps originally requested: "
                     + (str(asked) if asked else
                        "none -- `-s` was omitted, which is GenPipes for "
                        "every step of the protocol"))
        full = slot_table.step_range(slots.get("pipeline"),
                                      slots.get("protocol"))
        if full:
            names = slot_table.step_names(slots.get("pipeline"),
                                           slots.get("protocol"))
            lines.append(f"  this protocol's full step range: {full}  "
                         f"(step 1 is {names[0]}, step {len(names)} is "
                         f"{names[-1]})")
            lines.append("  That range is recorded from the installed "
                         "GenPipes -- do not run --help to rediscover it.")
        lines += [
            f"  failed jobs: {report['failed_total']} "
            f"across {report['steps_affected']} step(s)",
            "",
        ]
        if not report["findings"]:
            lines += ["No job wrote a log worth reading -- which is itself the "
                      "finding. Explain what that means for this run.", ""]
        for f in report["findings"]:
            # THE EXACT JOB, AND ITS SLURM ID. triage() has carried both since
            # it was written; this block printed neither, so the one identity
            # question a diagnosis must not get wrong was the one fact the
            # model had to reconstruct for itself from a job list. The id is
            # here too because it is the only globally unique handle -- it is
            # what sacct, squeue and the log filename all agree on, and a model
            # that wants to check a claim can quote it.
            lines.append(f"--- step {f['step']}: {f['count']} job(s) "
                         f"{f['state']} ---")
            if f.get("job"):
                lines.append(f"    failing job: {f['job']}"
                             + (f"  (Slurm job id {f['job_id']})"
                                if f.get("job_id") else ""))
                if f["count"] > 1:
                    lines.append(f"    {f['count'] - 1} other job(s) in this "
                                 f"step are in the same state; this is the "
                                 f"first of them.")
                else:
                    lines.append("    this is the ONLY job of this step in "
                                 "that state -- do not hedge about which one "
                                 "it was.")
            if f["maxrss"]:
                lines.append(f"    peak memory: {f['maxrss']}")
            if f["exit_code"]:
                lines.append(f"    exit code: {f['exit_code']}")
            # A cancelled job never started, so there is no log and no search
            # for one -- see runs.triage. Said plainly, because "not found on
            # disk" reads as a missing file and invites a hunt for it.
            if not f.get("ran"):
                lines.append("    no log: cancelled in the queue, never ran")
                lines.append("")
                continue
            lines.append(f"    log: {f['log'] or 'not found on disk'}")
            if f.get("script"):
                lines.append(f"    the script it was given: {f['script']}")
            if f["log_tail"]:
                lines.append("    tail of that log:")
                lines += [f"      {l}" for l in f["log_tail"].splitlines()]
            lines.append("")
        # ARTIFACTS NAMED, NOT PASTED.
        #
        # genpipes.md calls the .sh decisive and requires cross-checking the
        # config trace, and neither was reaching the model at all. Pasting both
        # for every finding would answer that by putting tens of kilobytes into
        # every prompt regardless of whether the question needs them. So the
        # paths are established deterministically -- that is evidence identity,
        # the same job resolve_log() does -- and whether opening one is worth a
        # round trip stays a decision for whoever is reasoning about the cause.
        # WHAT EACH FILE IS, mechanically. The type is stated because the last
        # time these were offered as bare paths the .sh was searched with an
        # ini-section pattern -- `^\[step\]` against a bash script -- which
        # returned nothing and cost a round trip. Naming the format is a fact
        # about the file; it prescribes nothing about what to look for in it.
        available = [f"    {f['script']}\n      a generated Bash submission "
                     f"script: the exact command line {f['step']} was run with, "
                     f"every flag and input path"
                     for f in report["findings"] if f.get("script")]
        if report.get("trace"):
            available.append(
                f"    {report['trace']}\n      a resolved GenPipes "
                f"configuration snapshot in ini format: every section after the "
                f"-c stack was merged, as of when this run was generated")
        if available:
            lines += ["Also on disk for this run, not quoted above:"]
            lines += available
            lines += ["",
                      "Read any of them with <execute> if your reasoning needs "
                      "it. Several files can go in ONE <execute> block -- a "
                      "batched read costs one round trip where separate ones "
                      "cost several.",
                      ""]
        # WHERE THE CONFIG VALUES CAME FROM.
        #
        # Only for the step the scheduler says broke first, which is a fact
        # runs._root_cause established from start times rather than a choice
        # made here. Without this the model was told a stack parsed at gate
        # time -- which for any run predating the flag_values fix is missing
        # entries, and was missing the one that mattered -- and had no way to
        # see what any of those files actually say. See genpipe/provenance.py
        # for why this stops at presenting the three observations.
        cause = status.root_cause if status is not None else None
        if cause and cause.get("step"):
            lines += provenance.lines(provenance.report(
                record, cause["step"], trace_path=report.get("trace"),
                workdir=record.get("workdir")))
        if question:
            lines += [f"The user specifically asks: {question}", ""]
        if slots.get("steps"):
            lines += [f"The run was originally submitted with -s {slots['steps']}.",
                      ""]
        lines += [
            diagnosis.SHAPE,
            "",
            diagnosis.RELAUNCH_RULE,
            "",
            "Do not resubmit anything now. Only use <execute> if you genuinely "
            "need to read a file that is not quoted above.",
        ]
        return "\n".join(lines)


def _epoch(stamp):
    """An ISO timestamp from the registry as a float, or None.

    Only a fallback: `submitted_since` is written as a float precisely so this
    is not needed, but records written before that field existed still have to
    be answerable about.
    """
    if not stamp:
        return None
    try:
        return datetime.datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return None


def _trace_values(trace_path, override):
    """What this run's config trace recorded for the keys an override changes.

    {section: {key: value}}, and {} when there is no trace or it says nothing
    about them. TRANSCRIPTION, in provenance.py's sense: the trace is GenPipes'
    own resolved snapshot of the stack it used, so reading a key out of it is
    reading a file, not concluding anything. Nothing here compares it to the
    proposed value or decides what the difference means.

    Read at diagnosis time because that is when the path is in hand and when
    the file is still the one that describes this run. A retry prepared a week
    later must not go looking for a trace that may by then belong to a
    different execution.
    """
    if not trace_path or not override:
        return {}
    out = {}
    for step, settings in override.items():
        found = provenance.effective(trace_path, step, keys=tuple(settings))
        if isinstance(found, dict) and found:
            out[step] = dict(found)
    return out


def _one_line(text, limit=140):
    """Squash a model's answer to one line for the run's note field.

    The <solution> wrapper is stripped: the tag is an artifact of how the agent
    talks to itself, and leaving it in means /history six weeks later reads as
    markup rather than as a finding.
    """
    flat = re.sub(r"</?solution>", " ", text or "")
    flat = " ".join(flat.split())
    return flat[:limit - 1] + "…" if len(flat) > limit else flat
