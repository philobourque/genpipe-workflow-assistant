#!/usr/bin/env python
"""cli._track()/_settled()/_briefed()/_at_the_gate(), with no model involved.

Everything exercised here is deterministic Python: intake parsing, the carried
prep.Preparation, and the text assembled for the agent. No LLM is invoked and no
LangGraph is built, so this cannot tell you what a real model would DO with what
it is handed -- only what it is handed, and what is decided without it.

That distinction is the whole point of this suite now. It used to assert the
opposite property: that `agent.run()` was NOT called while a required slot was
missing, because a deterministic panel loop (`_handle_gap`) answered the
question first and ended the turn. That loop is gone. It read "idk" as a
filename, could not see a dataset named in words rather than as a path, and
preempted the agent on every turn of a run being prepared -- so the model never
got to lead the conversation it was supposed to be leading.

What is left is memory, not control: parse what a sentence plainly states,
remember it across turns, and hand it over as facts. Every turn reaches the
model now, and the assertions below are written that way round.

Needs biomni importable, purely because cli.py imports `biomni.llm.get_llm` at
module scope -- nothing here calls it. Run in the biomni venv:

    ~/scratch/biomni-venv/bin/python tests/test_run_prep.py
"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Report
from genpipe import cli
from genpipe import intake
from genpipe import prep


def main():
    r = Report("run preparation: what the agent is handed, no model")

    # ------------------------------------------------------------------ #
    r.section("Scenario A: an incomplete request, launched next to an "
              "unrelated design.tsv -- nothing from the launch cwd may leak")

    launch_dir = tempfile.mkdtemp(prefix="unrelated-launch-")
    real_cwd = os.getcwd()
    try:
        open(os.path.join(launch_dir, "design.tsv"), "w").close()
        os.chdir(launch_dir)

        state = prep.Preparation()
        state, extra = cli._track(state, "I want to run an rnaseq pipeline on mouse data")

        r.equal("pipeline is recognised", state.pipeline, "rnaseq")
        r.equal("project_dir stays missing -- nothing was named",
                state.project_dir, None)
        r.truthy("what is settled is stated", extra and "rnaseq" in extra)
        r.truthy("the unrelated design.tsv is never surfaced",
                 extra is None or "design.tsv" not in extra)

        # The note is memory, not a script. It must not tell the model which
        # question to ask next: the model may have resolved the readset itself.
        r.truthy("no next question is dictated",
                 extra is None or "ONLY thing to ask" not in extra)

        text, context = cli._briefed(
            "I want to run an rnaseq pipeline on mouse data", None,
            state.project_dir)
        r.truthy("the briefed text never mentions the unrelated design.tsv",
                 "design.tsv" not in text)
    finally:
        os.chdir(real_cwd)
        shutil.rmtree(launch_dir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    r.section("Scenario B: a request that names a project directory")

    data_dir = tempfile.mkdtemp(prefix="named-project-")
    other_cwd = tempfile.mkdtemp(prefix="irrelevant-cwd-")
    real_cwd = os.getcwd()
    try:
        open(os.path.join(data_dir, "myReadset.tsv"), "w").close()
        # A design.tsv in the process's OWN cwd -- not the named directory --
        # must never surface. See AGENT-FIXES.md defect 1.
        open(os.path.join(other_cwd, "design.tsv"), "w").close()
        os.chdir(other_cwd)

        line = f"run ampliconseq, the readset is in {data_dir}"
        state = prep.Preparation()
        state, extra = cli._track(state, line)

        r.equal("the named directory becomes project_dir", state.project_dir, data_dir)
        r.equal("pipeline recognised too", state.pipeline, "ampliconseq")

        found = intake.candidates(state.project_dir)
        r.truthy("candidate discovery is rooted at the named directory",
                 any("myReadset.tsv" in p for p in found["readset"]))

        text, context = cli._briefed(line, None, state.project_dir)
        r.truthy("the readset in the named directory is offered",
                 "myReadset.tsv" in text)
        r.truthy("the unrelated design.tsv from the launch cwd never appears",
                 "design.tsv" not in text)
    finally:
        os.chdir(real_cwd)
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(other_cwd, ignore_errors=True)

    # ------------------------------------------------------------------ #
    r.section("memory across turns: nothing settled is asked about twice")

    work = tempfile.mkdtemp(prefix="carried-")
    try:
        readset = os.path.join(work, "readset.tsv")
        open(readset, "w").close()

        state = prep.Preparation()
        state, _ = cli._track(state, "I want to find inherited SNVs and small indels")
        r.equal("a scientific goal resolves the pipeline", state.pipeline, "dnaseq")
        r.equal("and the protocol, where it is unambiguous",
                state.protocol, "germline_snv")

        state, _ = cli._track(state, f"the data is in {work}")
        r.equal("a later turn adds the directory", state.project_dir, work)
        r.equal("without forgetting the pipeline", state.pipeline, "dnaseq")

        state, extra = cli._track(state, f"use {readset}")
        r.equal("and then the readset", state.readset, readset)
        r.equal("pipeline still remembered three turns later",
                state.pipeline, "dnaseq")
        r.truthy("all of it is handed over as settled",
                 extra and "dnaseq" in extra and "germline_snv" in extra
                 and readset in extra)
        r.truthy("and marked as not-to-be-asked-again",
                 extra and "not ask" in extra.lower())
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # ------------------------------------------------------------------ #
    r.section("a non-answer is never recorded as a value (the regression "
              "that made this rewrite necessary)")

    # "idk" typed at a readset question used to be classified as a plausible
    # slot value -- one word, so it passed the len(words) <= 3 test -- and
    # learned as the readset. prep.ready() then said the run was complete, the
    # gap loop exited, and the model was briefed "Readset: idk. Settled -- do
    # not ask for it again." The run reached the approval box with no readset.
    #
    # There is no classifier to fool now: a typed line is a message to the
    # model. What this asserts is the property underneath -- a filename is
    # learned only if it is really there.
    for non_answer in ("idk", "i dont know", "not sure", "dunno", "no idea",
                       "you pick", "help", "skip", "whatever the test data uses"):
        state = prep.Preparation()
        state.learn(pipeline="ampliconseq")
        state, _ = cli._track(state, non_answer)
        r.equal(f"{non_answer!r} is not learned as a readset", state.readset, None)

    state = prep.Preparation()
    state.learn(pipeline="rnaseq")
    state, _ = cli._track(state, "use missing_readset.tsv")
    r.equal("a filename that is not on disk is not learned either",
            state.readset, None)

    # ------------------------------------------------------------------ #
    r.section("at the gate: a question reaches the model instead of a refusal")

    class _Registry:
        """Two held runs, so the old global-ambiguity refusal would fire."""
        def __init__(self):
            self.runs = [{"name": "ampliconseq-0804", "thread_id": "t1"},
                         {"name": "rnaseq-stringtie-0804", "thread_id": "t2"}]

        def held(self):
            return list(self.runs)

        def held_for_thread(self, thread):
            for record in self.runs:
                if record["thread_id"] == str(thread):
                    return record
            return None

    class GateSpy:
        def __init__(self):
            self.registry = _Registry()
            self.resumed = []
            self._gate_note = None

        def resume(self, name, approved, feedback=None, on_step=None):
            self.resumed.append({"name": name, "approved": approved,
                                 "feedback": feedback})
            return {"status": "paused"}

    spy = GateSpy()
    question = "why did you propose the submission gate without asking me for more info"
    handled = cli._at_the_gate(spy, "t1", question)

    r.truthy("the line is dealt with at the gate", handled)
    r.equal("exactly one resume", len(spy.resumed), 1)
    r.equal("routed to THIS thread's run, with two held and no disambiguation asked",
            spy.resumed[0]["name"], "ampliconseq-0804")
    r.equal("as feedback, verbatim", spy.resumed[0]["feedback"], question)
    r.equal("and never as an approval", spy.resumed[0]["approved"], False)

    # The one thing still decided without the model.
    spy2 = GateSpy()
    r.truthy("an approval-shaped line is refused, not resumed",
             cli._at_the_gate(spy2, "t1", "looks good, go ahead"))
    r.equal("nothing was sent to the model for it", len(spy2.resumed), 0)

    spy3 = GateSpy()
    r.truthy("a thread with nothing held is not handled here",
             not cli._at_the_gate(spy3, "no-such-thread", "hello"))

    print()
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
