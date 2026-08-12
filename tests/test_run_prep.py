#!/usr/bin/env python
"""prep.track()/context() and cli._briefed()/_at_the_gate(), with no model.

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

What is left is narrower still. The pipeline/protocol/filename memory this
suite used to assert is gone too: matching a name proves it was TYPED, not
chosen ("should I use rnaseq or chipseq?" settled chipseq), and the model is
replayed the whole thread anyway, so nothing needed remembering for it. See
tests/test_prep.py, which is the offline guard on that.

What survives is PROVENANCE -- the directories somebody actually named, so
intake has somewhere to look other than the process's own cwd -- and the brief
assembled from them. Every turn reaches the model, and the assertions below are
written that way round.

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
from genpipe import ui


def drawn(fn, *args, **kwargs):
    """Call something that prints, and return what it printed, ANSI stripped."""
    import io
    import re
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    return re.sub(r"\033\[[0-9;]*[A-Za-z]", "", buf.getvalue())


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
        state, extra = prep.track(state, "I want to run an rnaseq pipeline on mouse data")

        # The pipeline is NOT recorded any more, even though "rnaseq" is right
        # there as a whole word. The same match fires on "should I use rnaseq
        # or chipseq?", and deciding which of those is a choice is a reading
        # the agent makes -- it has the sentence.
        r.equal("nothing is recorded from a sentence alone",
                state.as_dict(), {"directories": []})
        r.equal("and nothing is asserted to the model", extra, None)

        text, context = cli._briefed(
            "I want to run an rnaseq pipeline on mouse data", None,
            state.directory)
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
        state, extra = prep.track(state, line)

        r.equal("the named directory is remembered", state.directories, [data_dir])
        r.contains("as provenance, not as a verdict", extra, "mentioned so far")
        r.check("with no claim about what it contains",
                "readset" not in (extra or "").lower(), extra)

        found = intake.candidates(state.directory)
        r.truthy("candidate discovery is rooted at the named directory",
                 any("myReadset.tsv" in p for p in found["readset"]))

        text, context = cli._briefed(line, None, state.directory)
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

        second = os.path.join(work, "more")
        os.makedirs(second, exist_ok=True)

        state = prep.Preparation()
        state, _ = prep.track(state, "I want to find inherited SNVs and small indels")
        # A scientific goal used to resolve dnaseq AND germline_snv from a
        # regex table, then tell the model not to ask about either. A guessed
        # protocol produces a run that completes successfully and answers a
        # different question -- which is exactly what agent.py's own prompt
        # forbids the MODEL from doing.
        r.equal("a scientific goal resolves nothing deterministically",
                state.as_dict(), {"directories": []})

        state, _ = prep.track(state, f"the data is in {work}")
        r.equal("a named directory is remembered", state.directories, [work])

        state, extra = prep.track(state, f"and some more in {second}")
        r.equal("directories accumulate rather than overwrite",
                state.directories, [work, second])
        r.equal("the first mentioned stays primary", state.directory, work)
        r.contains("both are handed over", extra, second)
        r.check("with nothing marked not-to-be-asked-again",
                "not ask" not in extra.lower(), extra)
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
    # There is nothing left to fool: no line records a slot value at all now,
    # so the property holds for every input rather than for the ones a
    # classifier happened to reject.
    for non_answer in ("idk", "i dont know", "not sure", "dunno", "no idea",
                       "you pick", "help", "skip", "whatever the test data uses",
                       "use missing_readset.tsv", "readset.tsv"):
        state, extra = prep.track(prep.Preparation(), non_answer)
        r.equal(f"{non_answer!r} records nothing",
                state.as_dict(), {"directories": []})
        r.equal(f"and asserts nothing: {non_answer!r}", extra, None)

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

    # ------------------------------------------------------------------ #
    r.section("/model completes the closed set and leaves the open one alone")
    # Biomni validates get_llm()'s `source` against a fixed list and passes
    # `model` through untouched, so providers can be offered exhaustively and
    # model names cannot be offered at all without guessing.
    was = {k: os.environ.get(k) for k in
           ("GENPIPE_LLM_SOURCE", "GENPIPE_LLM_MODEL", "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY")}
    try:
        os.environ["GENPIPE_LLM_SOURCE"] = "Anthropic"
        os.environ["GENPIPE_LLM_MODEL"] = "claude-opus-5"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-not-a-placeholder"
        os.environ["OPENAI_API_KEY"] = "sk-..."      # .env.example shape
        offered = cli._run_names(None, "model")
        names = [v for v, _ in offered]
        notes = dict(offered)

        r.equal("every configurable provider is offered",
                names, [p[1] for p in cli.KNOWN_PROVIDERS])
        # Biomni knows eight sources; four of them need an endpoint or ambient
        # cloud credentials rather than a pasted key, so /model cannot switch
        # to them and completing them would offer a dead end.
        r.check("and the ones a bare key cannot reach are not",
                not {"AzureOpenAI", "Bedrock", "Ollama", "Custom"} & set(names))

        r.contains("the active provider is marked", notes["Anthropic"], "current")
        r.contains("with the model actually loaded, not the provider default",
                   notes["Anthropic"], "claude-opus-5")
        # A provider with no key is shown rather than hidden: "why is OpenAI
        # missing?" is a worse question to leave someone with than a row that
        # names the command that fixes it.
        r.contains("a provider with no key is offered, and says so",
                   notes["OpenAI"], "no key yet")

        # The point of the whole thing: the second word is free-form, because
        # Biomni never checks it.
        editor = ui._Editor([("model", "[provider [model]]", "", "")], [],
                            initial="/model ",
                            arguments=lambda c: cli._run_names(None, c))
        r.equal("the provider argument completes",
                [v for v, _ in editor.arg_matches()], names)
        typed = ui._Editor([("model", "", "", "")], [], initial="/model g",
                           arguments=lambda c: cli._run_names(None, c))
        r.equal("and filters as you type, case-insensitively",
                [v for v, _ in typed.arg_matches()], ["Gemini", "Groq"])
        after = ui._Editor([("model", "", "", "")], [], initial="/model Anthropic ",
                           arguments=lambda c: cli._run_names(None, c))
        r.equal("but the model name after it is typed, not chosen from a menu",
                after.arg_matches(), None)

        r.equal("a command whose argument we cannot enumerate still says so",
                cli._run_names(None, "track"), None)

        # ---------------------------------------------------------------- #
        r.section("/verbose flips, rather than always meaning on")
        from genpipe import display as disp
        was_verbose = disp.VERBOSE
        try:
            disp.set_verbose(False)
            disp._folded.clear()
            out = drawn(cli._cmd_verbose, None, [])
            r.equal("bare /verbose from folded turns it on", disp.VERBOSE, True)
            # It used to mean "on" unconditionally, so pressing it again did
            # nothing and then said so.
            out_back = drawn(cli._cmd_verbose, None, [])
            r.equal("and pressing it again turns it off", disp.VERBOSE, False)
            r.contains("saying which way it went", out_back, "folded away")

            r.equal("'on' still parses for anyone who says it",
                    (drawn(cli._cmd_verbose, None, ["on"]), disp.VERBOSE)[1], True)
            r.equal("and so does 'off'",
                    (drawn(cli._cmd_verbose, None, ["off"]), disp.VERBOSE)[1], False)

            # Nothing folded is not an event -- it means the session has done no
            # working yet, which the person can see. It used to cost a block.
            disp._folded.clear()
            fresh = drawn(cli._cmd_verbose, None, [])
            r.check("a session with no working yet says nothing about it",
                    "othing has been folded" not in fresh)
            r.check("and confirms the setting in one message",
                    fresh.count("▌") == 2)

            # The count belongs to the confirmation, not to a header of its own.
            disp.set_verbose(False)
            disp._folded.clear()
            disp._folded.append({"kind": "code", "text": "ls x", "label": "READ"})
            one = drawn(cli._cmd_verbose, None, [])
            r.contains("replayed work is counted where it is confirmed",
                       one, "1 step replayed")
            r.check("and the count agrees with itself", "1 step(s)" not in one)
        finally:
            disp.set_verbose(was_verbose)
            disp._folded.clear()

        # ---------------------------------------------------------------- #
        r.section("/model proves a model before it accepts one")
        # A provider only rejects an unknown model when a request is made --
        # get_llm() builds a client and returns -- so `/model Anthropic
        # haiku-4-5` used to be confirmed on screen, written to .env, and then
        # 404 a turn later from inside the agent loop.
        class Boom(Exception):
            def __init__(self, msg, status=None):
                super().__init__(msg)
                self.status_code = status

        class FakeLLM:
            def __init__(self, model, exc=None):
                self.model, self.exc = model, exc
            def invoke(self, _):
                if self.exc:
                    raise self.exc
                return "ok"

        class FakeAgent:
            pass

        r.equal("a live model passes",
                cli._probe_llm(FakeLLM("m"))[0], cli._MODEL_OK)
        r.equal("a 404 is the model not existing",
                cli._probe_llm(FakeLLM("m", Boom("nope", 404)))[0],
                cli._MODEL_REJECTED)
        r.equal("and is recognised from the body when there is no status",
                cli._probe_llm(FakeLLM("m", Boom("{'type': 'not_found_error'}")))[0],
                cli._MODEL_REJECTED)
        r.equal("a rejected key is refused too, not blamed on the model",
                cli._probe_llm(FakeLLM("m", Boom("bad", 401)))[1],
                "the key was rejected")
        # The distinction the three-state return exists for: a rate limit says
        # nothing about whether the model is real, and refusing the switch on
        # that evidence would strand somebody behind a transient error.
        r.equal("a rate limit leaves the verdict open, not negative",
                cli._probe_llm(FakeLLM("m", Boom("slow down", 429)))[0],
                cli._MODEL_UNVERIFIED)
        r.equal("as does a network failure",
                cli._probe_llm(FakeLLM("m", OSError("dns")))[0],
                cli._MODEL_UNVERIFIED)

        writes = []
        scripted = {}
        real = (cli._write_env_var, cli.get_llm, cli._drop_sampling_params)
        cli._write_env_var = lambda k, v: writes.append((k, v))
        cli.get_llm = lambda model, **kw: FakeLLM(model, scripted.get(model))
        cli._drop_sampling_params = lambda llm, source: llm
        try:
            agent = FakeAgent()
            agent.llm = FakeLLM("claude-sonnet-5")

            scripted["haiku-4-5"] = Boom("model: haiku-4-5", 404)
            out = drawn(cli._cmd_model, agent, ["Anthropic", "haiku-4-5"])
            r.contains("a bad name is named, with what was wrong with it",
                       out, "no such model")
            r.equal("the working model is left in place",
                    agent.llm.model, "claude-sonnet-5")
            # The half that turned a typo into a lasting problem: the bad name
            # reached .env, survived the restart, and came back on the next
            # launch's banner.
            r.equal("and nothing is written to .env", writes, [])
            r.contains("the row says what is still in use", out, "Still using")

            out = drawn(cli._cmd_model, agent, ["Anthropic", "claude-haiku-4-5"])
            r.equal("a good name is applied", agent.llm.model, "claude-haiku-4-5")
            r.equal("and only then persisted",
                    writes, [("GENPIPE_LLM_SOURCE", "Anthropic"),
                             ("GENPIPE_LLM_MODEL", "claude-haiku-4-5")])
            r.contains("named the way the banner names it", out, "Anthropic · claude-haiku-4-5")

            writes.clear()
            scripted["claude-opus-5"] = Boom("rate limited", 429)
            out = drawn(cli._cmd_model, agent, ["Anthropic", "claude-opus-5"])
            r.equal("an unreachable provider does not block the switch",
                    agent.llm.model, "claude-opus-5")
            r.check("which is persisted like any other", writes != [])
            r.contains("but the listing says the check did not happen",
                       out, "Could not reach")
        finally:
            cli._write_env_var, cli.get_llm, cli._drop_sampling_params = real
    finally:
        for k, v in was.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print()
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
