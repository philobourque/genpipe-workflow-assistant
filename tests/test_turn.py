#!/usr/bin/env python
"""Where a conversational turn ends, and why it is not wherever the model stops.

THE TWO FAILURES THIS SUITE EXISTS FOR, both seen live on the cluster.

    > Can you show me all my runs?

    ...the real /list panel, twenty-one runs, correctly rendered...

    No runs currently recorded — nothing active and nothing needing
    attention. If you'd like, I can check the full run history...

and

    > What fix should I choose for a rerun?

    ...413 seconds, 36 model calls, five capabilities, 185 of those seconds
    spent AFTER the last panel was already on screen...

Neither is a prompting problem, and neither was fixed by prompting -- the
prompt had already been patched twice ("Do not call it again; say what
happened", "comment on it rather than asking for it again") and the model went
on to inspect_jobs anyway. It was one edge:

    workflow.add_edge("capability", "generate")

unconditional, so a capability that had just drawn the complete user-facing
answer went back to the model exactly like a tool returning a number. The
canonical screen was an intermediate observation.

WHAT IS ASSERTED HERE is the routing and the state, never the wording of a
reply: the number of model calls a turn spends, which capabilities ran, and
where the graph ended. A test that matched phrases would pass on a model that
had learned to be quiet and fail on one that had not, which is the wrong thing
to be measuring.

The model is scripted, so `llm.calls` is exact. One call means: the model was
asked once, wrote one capability call, and was never asked again.

Run:  GENPIPE_FAKE=1 python tests/test_turn.py
"""
import os
import shutil
import sys
import tempfile

from harness import Report, ScriptedLLM, execute_block, solution

from genpipe import agent as agent_module
from genpipe import capabilities
from genpipe import display
from genpipe import fakecluster
from genpipe import runs as runs_store
from genpipe import cli
from genpipe.cli import build_agent


def call(text):
    """A model turn that is exactly one capability call, as the prompt asks."""
    return execute_block(text)


def fresh(work, script):
    agent = build_agent(path=work)
    agent.use_tool_retriever = False
    agent.llm = ScriptedLLM(script)
    seen = []
    inner = agent._run_capability

    def traced(spec, args):
        seen.append(spec.name)
        return inner(spec, args)

    agent._run_capability = traced
    agent.capabilities_seen = seen
    return agent


def seed(store, work):
    """Enough registry for /list, /check and /jobs to have something to draw."""
    os.makedirs(store, exist_ok=True)
    reg = runs_store.Registry(store)
    reg.hold("study-a", "t-a",
             {"command": "bash cmd.sh",
              "generated": "#!BASH\nmodule load x && genpipes rnaseq -t stringtie "
                           "-c a.ini -r readset.tsv -g cmd.sh",
              "slots": {"pipeline": "rnaseq", "protocol": "stringtie",
                        "inis": ["a.ini"], "readset": "readset.tsv"}},
             work)
    reg.hold("study-b", "t-b",
             {"command": "bash cmd.sh",
              "generated": "#!BASH\nmodule load x && genpipes rnaseq -t stringtie "
                           "-c a.ini -r readset.tsv -g cmd.sh",
              "slots": {"pipeline": "rnaseq", "protocol": "stringtie",
                        "inis": ["a.ini"], "readset": "readset.tsv"}},
             work)
    return reg


def main():
    r = Report("where a conversational turn ends")
    work = tempfile.mkdtemp(prefix="genpipe_turn_")
    store = os.path.join(work, "biomni_data")
    envdir = tempfile.mkdtemp(prefix="genpipe_turn_env_")
    cli.ENV_PATH = __import__("pathlib").Path(envdir) / ".env"
    prev = os.getcwd()
    try:
        with fakecluster.session("failed-oom"):
            os.chdir(work)

            # ============================================================== #
            r.section("the table says which actions answer the person")
            # THE STRUCTURAL FACT the routing rests on. Every capability today
            # draws the same panel its slash command draws; the flag exists so
            # that a future lookup with no screen of its own is not mistaken
            # for an answer.
            for name, spec in sorted(capabilities.TABLE.items()):
                r.check(f"{name} renders its own answer", spec.renders, spec)
            r.contains("and the model is told so, from the table itself",
                       capabilities.protocol(), "the turn ENDS there")
            r.contains("including how to say it needs more",
                       capabilities.protocol(), f"{capabilities.CONTINUE}=True")

            # ============================================================== #
            r.section("the prompt and the renderer agree on what <solution> "
                      "may contain")
            # A DRIFT TEST, not a wording test. display._prose renders exactly
            # three markers; the prompt asks for exactly those three. Either
            # side can be reworded freely -- what must not happen is one side
            # growing or losing a marker on its own, because the failure that
            # produces is invisible: the model writes something reasonable and
            # the terminal prints it as punctuation.
            contract = agent_module.SYSTEM_PROMPT
            for marker, why in (("**", "emphasis"),
                                ("`", "inline code"),
                                ("- ", "a leading bullet")):
                r.check(f"the prompt names {why}", marker in contract, why)

            # And the renderer really does handle each of them, so the prompt
            # is not promising a rendering that does not exist.
            r.equal("emphasis renders",
                    display._prose("**a**"), [f"{display.BOLD}a{display.RESET}"])
            r.equal("inline code renders",
                    display._prose("`a`"),
                    [f"{display.SECONDARY}a{display.RESET}"])
            r.equal("a bullet renders",
                    display._prose("- a"),
                    [f"{display.GREY}\u2022{display.RESET} a"])

            # The unsupported forms are named as unsupported. Concepts, not
            # sentences -- the wording around them is free to change.
            for word in ("heading", "table", "nested", "fence"):
                r.check(f"{word}s are addressed", word in contract.lower(), word)

            # The boundary that keeps this honest: nothing else is rendered, so
            # a fourth marker added to the renderer without a line in the
            # prompt fails here.
            for unsupported in ("## Heading", "| a | b |", "> quote",
                                "1. numbered", "~~struck~~", "_italic_"):
                r.equal(f"{unsupported!r} is printed as written",
                        display._prose(unsupported), [unsupported])

            r.section("`more` is a statement about the turn, not an argument")
            args, more = capabilities.continues({"name": "x", "more": True})
            r.equal("it is stripped before the handler sees it", args, {"name": "x"})
            r.equal("and read as a continuation", more, True)
            r.equal("absent means finished",
                    capabilities.continues({"name": "x"})[1], False)
            r.equal("and so does anything that is not literally true",
                    capabilities.continues({"name": "x", "more": "maybe"})[1],
                    False)
            r.equal("no entry declares it",
                    [n for n, c in capabilities.TABLE.items()
                     if capabilities.CONTINUE in c.args], [])
            r.equal("yet every entry accepts it",
                    [n for n in capabilities.TABLE
                     if capabilities.validate(n, {"name": "x", "more": True})[1]
                     and "no more" in str(
                         capabilities.validate(n, {"name": "x", "more": True})[1])],
                    [])

            # ============================================================== #
            seed(store, work)

            r.section("show me all my runs — one call, and the turn is over")
            # THE CONTRADICTION CASE. The script has a second entry precisely
            # so that a turn which kept going would find one and spend it; a
            # turn that ends leaves it unused.
            agent = fresh(work, [call("list_runs()"),
                                 solution("There are no runs recorded.")])
            status = agent.run("Can you show me all my runs?", thread_id="c1")
            r.equal("the model was asked exactly once", agent.llm.calls, 1)
            r.equal("one capability ran", agent.capabilities_seen, ["list_runs"])
            r.equal("and the graph ended rather than pausing",
                    status.get("status"), "done")
            said = "\n".join(str(getattr(m, "content", "")) for m in
                             agent._history(agent._config("c1")))
            r.check("no model prose followed the panel",
                    "There are no runs recorded." not in said, said[-400:])

            r.section("...and what the model is handed cannot contradict it")
            # The other half of the same live failure, and it outlived the
            # routing fix: submissions() returned None whether it had drawn
            # twenty-one runs or none, so the observation said "found nothing
            # to report" about a full screen. It is the count now, tallied
            # from the rows the panel was drawn from.
            r.contains("the observation carries what the panel shows", said,
                       "2 run(s) shown to the user")
            r.check("and does not claim there was nothing",
                    "no runs to show" not in said, said[-400:])

            r.section("check on a run — no automatic diagnose, no automatic jobs")
            agent = fresh(work, [call('check_run(name="study-a")'),
                                 call('diagnose_run(name="study-a")'),
                                 solution("done")])
            agent.run("Check on study-a", thread_id="c2")
            r.equal("the model was asked once", agent.llm.calls, 1)
            r.equal("check_run ran and nothing else",
                    agent.capabilities_seen, ["check_run"])
            r.check("diagnose was not reached for it",
                    "diagnose_run" not in agent.capabilities_seen)

            r.section("show me the jobs — one call, and the turn is over")
            agent = fresh(work, [call('inspect_jobs(name="study-a")'),
                                 call('show_run(name="study-a")'),
                                 solution("done")])
            agent.run("I would like to see all the jobs for this particular run",
                      thread_id="c3")
            r.equal("the model was asked once", agent.llm.calls, 1)
            r.equal("inspect_jobs ran and nothing else",
                    agent.capabilities_seen, ["inspect_jobs"])

            r.section("a diagnosis is not followed by an inspection")
            # THE 413-SECOND CASE. The script would happily go on to
            # inspect_jobs; the routing is what stops it.
            agent = fresh(work, [call('diagnose_run(name="study-a")'),
                                 call('inspect_jobs(name="study-a")'),
                                 call('show_run(name="study-a")'),
                                 solution("done")])
            agent.run("What fix should I choose for a rerun?", thread_id="c4")
            r.equal("the model was asked once", agent.llm.calls, 1)
            r.equal("the diagnosis ran and nothing followed it",
                    agent.capabilities_seen, ["diagnose_run"])
            r.check("no jobs panel was drawn after it",
                    "inspect_jobs" not in agent.capabilities_seen)
            r.check("and nothing was prepared on the user's behalf",
                    agent.registry.get("study-a-2") is None)

            # ============================================================== #
            r.section("what a diagnosis tells the model it found")
            # THE DEAD BRANCH. diagnose() returns a DICT -- the gate status
            # with the parsed answer and this run's scheduler facts on it --
            # and the note builder tested `hasattr(result, "counts")`, which a
            # dict has not got. So the most expensive lookup in the product
            # reported itself as "diagnose_run completed for <name>", and a
            # model asked to carry on from that had nothing to carry on from.
            bare = object.__new__(agent_module.GenpipeA1)
            note = agent_module.GenpipeA1._capability_note
            spec = capabilities.TABLE["diagnose_run"]

            def result_for(name, verdict, counts, total, step, count, state,
                           cause, override, uncertain):
                return {"status": "done", "final": "...",
                        "evidence": {"name": name, "verdict": verdict,
                                     "counts": counts, "total": total,
                                     "root_cause": {"step": step,
                                                    "count": count,
                                                    "state": state}},
                        "diagnosis": {"shaped": True, "cause": cause,
                                      "override": override,
                                      "uncertain": uncertain}}

            FASTPASS = result_for(
                "dnaseq-somatic-fastpass-0805", "failed, nothing still running",
                {"CANCELLED": 32, "COMPLETED": 13, "TIMEOUT": 1}, 46,
                "gatk_sam_to_fastq", 1, "TIMEOUT",
                "killed at its 00:10:00 walltime, still streaming reads from "
                "the tumour BAM; cit.ini lowers this step to 0:10:00",
                {"gatk_sam_to_fastq": {"cluster_walltime": "35:00:00"}},
                ["whether 35:00:00 is actually sufficient for this input",
                 "whether memory pressure at 99.3% contributed"])
            WALLTIMEFAIL = result_for(
                "Test_walltimefail", "failed, nothing still running",
                {"CANCELLED": 43, "COMPLETED": 1, "TIMEOUT": 2}, 46,
                "gatk_sam_to_fastq", 2, "TIMEOUT",
                "killed at 00:01:00 after running 00:01:01",
                {"gatk_sam_to_fastq": {"cluster_walltime": "0:10:00"}},
                ["whether 0:10:00 is enough for a full BAM"])

            said = note(bare, spec, {"name": "dnaseq-somatic-fastpass-0805"},
                        FASTPASS)
            r.check("a dict-shaped result reaches the diagnosis branch",
                    "completed for" not in said, said)
            r.contains("it names the run it diagnosed", said,
                       "dnaseq-somatic-fastpass-0805")
            r.contains("with the scheduler's verdict", said,
                       "failed, nothing still running")
            r.contains("the tally", said, "32 cancelled, 13 completed, 1 timeout")
            r.contains("of how many were submitted", said, "of 46 submitted")
            r.contains("the earliest independent failure", said,
                       "Earliest independent failure: gatk_sam_to_fastq")
            r.contains("what the diagnosis concluded", said, "00:10:00 walltime")
            r.contains("the fix it proposed, as a section and a setting", said,
                       "[gatk_sam_to_fastq] cluster_walltime = 35:00:00")
            r.contains("the caveat that travels with it", said,
                       "Not established: whether 35:00:00")
            r.contains("and how many more there were", said, "(+1 more)")
            r.check("and it stays an observation, not a second panel",
                    "▌" not in said and len(said) < 800, len(said))

            r.section("...and every value in it belongs to that run")
            other = note(bare, spec, {"name": "Test_walltimefail"}, WALLTIMEFAIL)
            # Tokens unique to the OTHER run: its name, its tally and its
            # walltime. 0:10:00 is deliberately not among them -- it is
            # 0805's own merged value and appears in 0805's cause.
            for stray in ("Test_walltimefail", "43 cancelled", "00:01:0"):
                r.check(f"{stray!r} is not in the fastpass note",
                        stray not in said, said)
            for stray in ("dnaseq-somatic-fastpass-0805", "32 cancelled",
                          "35:00:00", "99.3%"):
                r.check(f"{stray!r} is not in the walltimefail note",
                        stray not in other, other)
            r.contains("each note names its own run", other, "Test_walltimefail")
            r.contains("with its own tally", other, "43 cancelled")
            r.contains("and its own proposed value", other, "0:10:00")

            r.section("a diagnosis with nothing to report says so by omission")
            thin = note(bare, spec, {"name": "quiet"},
                        {"evidence": {"name": "quiet", "verdict": "",
                                      "counts": {}, "total": 0,
                                      "root_cause": {}},
                         "diagnosis": {}})
            r.check("no invented tally", "submitted" not in thin, thin)
            r.check("no invented fix", "Proposed override" not in thin, thin)
            r.contains("but it still names the run", thin, "quiet")
            r.contains("and still says the screen has the answer", thin,
                       "already on screen")
            r.contains("a refusal is still a refusal",
                       note(bare, spec, {"name": "gone"}, None),
                       "could not answer")

            # ============================================================== #
            r.section("an explicitly multi-intent request still chains")
            # THE PROPERTY THAT MUST SURVIVE. "check it, and diagnose it if it
            # failed" is genuinely two surfaces, and the model says so in the
            # call rather than in prose -- which is what makes it a decision
            # the graph can act on rather than a sentence something has to
            # interpret.
            agent = fresh(work, [call('check_run(name="study-a", more=True)'),
                                 call('diagnose_run(name="study-a")'),
                                 call('show_run(name="study-a")'),
                                 solution("done")])
            agent.run("check this run and, if it failed, diagnose it",
                      thread_id="c5")
            r.equal("the chain the model asked for happened",
                    agent.capabilities_seen, ["check_run", "diagnose_run"])
            r.equal("which cost one model call per link", agent.llm.calls, 2)
            r.check("and the last surface ended the turn",
                    "show_run" not in agent.capabilities_seen)

            r.section("the comparison a useful observation makes possible")
            # "And what about the other run — same problem?" is the request
            # this branch exists for: diagnose_run(..., more=True) on run A,
            # then an answer that compares it with run B WITHOUT going and
            # looking again. The script's third entry is a second capability
            # the model does not need and must not reach for; the fourth is the
            # answer. If the observation were the old content-free one, the
            # model would have nothing to compare with and this is where that
            # would show.
            agent = fresh(work, [
                call('diagnose_run(name="study-a", more=True)'),
                solution("study-a hit the same wall as study-b: both are "
                         "gatk_sam_to_fastq timeouts."),
                call('inspect_jobs(name="study-a")'),
            ])
            agent.run("and what about the other run — same problem?",
                      thread_id="c8")
            r.equal("the diagnosis ran once", agent.capabilities_seen,
                    ["diagnose_run"])
            r.equal("the model was asked once more, and finished there",
                    agent.llm.calls, 2)
            r.check("no second lookup was needed to answer",
                    "inspect_jobs" not in agent.capabilities_seen)
            history = "\n".join(str(getattr(m, "content", "")) for m in
                                 agent._history(agent._config("c8")))
            r.contains("and what came back named the run", history, "study-a")
            r.check("as an observation, not as a rendered panel",
                    "<observation>" in history and "▌" not in history)

            r.section("a diagnosis that was not asked to continue still ends")
            agent = fresh(work, [call('diagnose_run(name="study-a")'),
                                 solution("...")])
            agent.run("why did study-a fail?", thread_id="c9")
            r.equal("one model call, no continuation", agent.llm.calls, 1)
            r.equal("and one capability", agent.capabilities_seen,
                    ["diagnose_run"])

            r.section("a call the model got wrong is never terminal")
            # Nothing was rendered, so there is nothing to end the turn on --
            # the complaint has to reach the model so it can fix what it wrote.
            agent = fresh(work, [call('check_run(nmae="study-a")'),
                                 call('check_run(name="study-a")'),
                                 solution("done")])
            agent.run("check study-a", thread_id="c6")
            r.equal("the refusal went back and the corrected call ran",
                    agent.capabilities_seen, ["check_run"])
            r.equal("which took a second model call", agent.llm.calls, 2)

            r.section("prose that is not a call is untouched by any of this")
            # The turn-ending rule is about capabilities. A question answered
            # from what the model already knows still ends the way it always
            # did, through <solution>.
            agent = fresh(work, [solution("A walltime is the time limit Slurm "
                                          "gives a job.")])
            agent.run("what does cluster_walltime mean?", thread_id="c7")
            r.equal("no capability ran", agent.capabilities_seen, [])
            r.equal("and the model answered in one call", agent.llm.calls, 1)
    finally:
        os.chdir(prev)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(envdir, ignore_errors=True)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
