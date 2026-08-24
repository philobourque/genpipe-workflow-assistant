#!/usr/bin/env python
"""The capability mechanism: a dispatcher for calls the MODEL wrote.

THE ACCEPTANCE CRITERION THIS SUITE EXISTS FOR is the negative one. The
capability layer must not become an intent router -- nothing in it may read
what a person typed and decide which action that sentence corresponds to. So
the first section feeds the parser every sentence anybody would reach for if
they were building exactly the thing we are refusing to build, and asserts it
produces nothing at all.

That property is structural rather than defended by a list: the parser is only
ever handed gate.extract_pending_code(), which reads the <execute> block out of
the LAST message -- and at the point the router runs, the last message is the
model's reply. A user's sentence has no path to this code. The tests below
check the property anyway, because "there is no path" is the kind of claim that
stops being true one refactor later.

What the rest of the suite covers:

  * the closed table -- an invented name is refused, not run
  * argument validation before any handler is reached
  * the inert state, which is how 7A ships: no names, no routing, no node
  * that the capability and the slash command call the SAME method, rather
    than being two implementations of one idea

Run:  python tests/test_capability.py
"""
import re
import sys

from harness import Report

from genpipe import capabilities, gate


# Sentences a person might type. EVERY ONE of these contains a word that a
# keyword router would have jumped on -- check, jobs, failed, diagnose,
# monitor, cancel, history. None of them may produce a capability, because
# none of them is a call the model made.
PROSE = [
    "how is run-x doing?",
    "what's the status of run-x?",
    "why did run-x fail?",
    "which jobs failed in run-x?",
    "show me every job in run-x",
    "can you diagnose runs?",
    "what does /diagnose do?",
    "what does cancel do?",
    "how does /check work?",
    "can runs be monitored?",
    "why do jobs fail in Slurm?",
    "check_run",
    "I want to check_run on something",
    "show me my runs",
    "what have I run previously?",
    "diagnose_run is the one that reads logs, right?",
]


def main():
    r = Report("capabilities: a dispatcher, not an intent router")

    names = capabilities.NAMES

    # ------------------------------------------------------------------ #
    r.section("prose is never a capability call")

    for line in PROSE:
        r.equal(f"{line[:44]!r}", gate.capability_request(line, names), None)

    # THE STRUCTURAL PROPERTY, checked where it actually lives.
    #
    # The parser reads code; the question is what code it is ever given. The
    # router hands it gate.extract_pending_code(), which pulls the <execute>
    # block out of the LAST message -- so a user's sentence, which arrives as
    # a message with no <execute> in it, yields nothing to parse. Even a
    # sentence that spells a capability call out in full.
    class Msg:
        def __init__(self, content):
            self.content = content

    # Including the one shape the PARSER alone cannot defend against: a
    # sentence that spells a call out in full. capability_request is a parser
    # of CODE -- handed code-shaped text it will find the call, and that is
    # correct behaviour for a parser. What makes it safe is that this text
    # never arrives as code, because a user message has no <execute> block.
    # The defence lives here, at the extraction, not in the regex.
    for line in PROSE + ["please run check_run(name='run-x') for me",
                         "should I use check_run(name='run-x') or /check?"]:
        pending = gate.extract_pending_code([Msg(line)])
        r.equal(f"no code to parse from {line[:34]!r}", pending, None)

    # And the same sentence, once the MODEL puts it in a block, is a call --
    # which is the distinction the whole design rests on. Who wrote it decides,
    # not what it says.
    pending = gate.extract_pending_code(
        [Msg('<execute>\ncheck_run(name="run-x")\n</execute>')])
    r.truthy("the model's own block does yield code", pending)
    r.equal("and that is a capability", gate.capability_request(pending, names),
            {"capability": "check_run", "args": {"name": "run-x"}})

    # ------------------------------------------------------------------ #
    r.section("narration is not execution")

    # The model explains its own work constantly. A capability named inside a
    # print() or an echo must not run, the same rule is_submission() follows.
    for code in ('print("check_run(name=\'x\')")',
                 'echo "diagnose_run(name=\'x\')"',
                 '# check_run(name="x")',
                 'cat << EOF\ncheck_run(name="x")\nEOF'):
        r.equal(f"narrated: {code[:36]!r}",
                gate.capability_request(code, names), None)

    # ------------------------------------------------------------------ #
    r.section("real calls parse")

    got = gate.capability_request('check_run(name="run-x")', names)
    r.equal("a simple call", got, {"capability": "check_run",
                                   "args": {"name": "run-x"}})
    got = gate.capability_request('inspect_jobs(name="run-x", failed=True)', names)
    r.equal("booleans are booleans", got["args"]["failed"], True)
    got = gate.capability_request("check_run(name='run-x')", names)
    r.equal("single quotes too", got["args"]["name"], "run-x")
    got = gate.capability_request('list_runs()', names)
    r.equal("no arguments is fine", got, {"capability": "list_runs", "args": {}})
    got = gate.capability_request('diagnose_run(name=f"run-{n}")', names)
    r.truthy("an f-string prefix does not break it", got)

    # ------------------------------------------------------------------ #
    r.section("the table is closed")

    r.equal("an invented name is not a capability",
            gate.capability_request('summarise_everything(name="x")', names), None)
    r.equal("ordinary python is untouched",
            gate.capability_request('os.path.join("a", "b")', names), None)
    r.equal("and so is a shell command",
            gate.capability_request('ls -la /tmp', names), None)

    spec, why = capabilities.validate("summarise_everything", {})
    r.equal("validate refuses an unknown name", spec, None)
    r.contains("naming the real ones", why, "check_run")

    # ------------------------------------------------------------------ #
    r.section("arguments are checked before any handler runs")

    spec, why = capabilities.validate("check_run", {})
    r.equal("a missing required argument is refused", spec, None)
    r.contains("saying which", why, "name")

    spec, why = capabilities.validate("check_run", {"nmae": "x"})
    r.equal("a typo'd argument is refused", spec, None)
    r.contains("naming what it does take", why, "name")

    spec, why = capabilities.validate("check_run", {"name": "   "})
    r.equal("whitespace is not a run name", spec, None)

    spec, why = capabilities.validate("check_run", {"name": "run-x"})
    r.truthy("a good call validates", spec)
    r.equal("with nothing to complain about", why, None)

    spec, why = capabilities.validate("inspect_jobs",
                                      {"name": "x", "failed": True})
    r.truthy("optional arguments are allowed", spec)

    # ------------------------------------------------------------------ #
    r.section("only read-only capabilities exist, and only they are enabled")

    for name, spec in capabilities.TABLE.items():
        r.equal(f"{name} reads only", spec.kind, capabilities.READS)
        r.check(f"{name} is enabled", spec.enabled)
    r.equal("nothing that mutates is in the table at all",
            [n for n, c in capabilities.TABLE.items()
             if c.kind != capabilities.READS], [])
    for forbidden in ("approve", "approve_run", "cancel_run", "hold_jobs",
                      "reject_run", "adopt_external_run", "propose_submission",
                      "modify_run", "hide_run"):
        r.equal(f"{forbidden} is not reachable", capabilities.get(forbidden), None)

    # ------------------------------------------------------------------ #
    r.section("inert until switched on")

    # How 7A ships. With no names the parser answers None for everything, the
    # router branch is never taken, and the node cannot be reached.
    for code in ('check_run(name="x")', 'list_runs()', 'diagnose_run(name="x")'):
        r.equal(f"off: {code}", gate.capability_request(code, frozenset()), None)
        r.equal(f"off (None): {code}", gate.capability_request(code, None), None)

    # ------------------------------------------------------------------ #
    r.section("the catalogue is generated, not written twice")

    text = capabilities.catalogue()
    for name, spec in capabilities.TABLE.items():
        if spec.enabled:
            r.contains(f"{name} is offered to the model", text, name)
    # It says what each action DOES, never when to reach for one. A summary
    # phrased as "call this when the user asks..." would be an intent rule in
    # prose, which is the thing this design refuses.
    lowered = text.lower()
    for phrase in ("when the user", "if the user", "use this when",
                   "call this when", "in response to"):
        r.check(f"the catalogue does not say {phrase!r}", phrase not in lowered)

    # ------------------------------------------------------------------ #
    r.section("one implementation, two doors")

    # The rule that makes "natural language and slash commands share the
    # underlying functionality" a fact rather than a hope: every capability is
    # bound to the SAME agent method the slash handler calls. Checked by
    # source, because a binding that quietly grew its own logic is exactly the
    # drift this is guarding against.
    import inspect

    from genpipe import agent as agent_module
    from genpipe import cli as cli_module

    source = inspect.getsource(agent_module.GenpipeA1._capability_handlers)
    for capability_name, method in (("check_run", "self.check("),
                                    ("inspect_jobs", "self.jobs("),
                                    ("diagnose_run", "self.diagnose("),
                                    ("list_runs", "self.submissions("),
                                    ("run_history", "self.history(")):
        r.contains(f"{capability_name} calls {method}...", source, method)

    # And the slash command calls the same one.
    for handler, method in ((cli_module._cmd_check, "agent.check("),
                            (cli_module._cmd_jobs, "agent.jobs("),
                            (cli_module._cmd_diagnose, "agent.diagnose("),
                            (cli_module._cmd_list, "agent.submissions("),
                            (cli_module._cmd_history, "agent.history(")):
        r.contains(f"...and so does {handler.__name__}",
                   inspect.getsource(handler), method)

    # The table and the bindings must cover exactly the same names, or one
    # side has grown an entry the other has never heard of.
    bound = set(re.findall(r'"([a-z_]+)":\s*lambda', source))
    r.equal("the table and the handlers agree exactly",
            bound, set(capabilities.TABLE))

    # ------------------------------------------------------------------ #
    r.section("the prompt section describes, it does not prescribe")

    # The section is generated, so it can be checked without a model. What it
    # must NOT contain is any mapping from a phrase to an action -- that is the
    # deterministic intent routing this design refuses, and putting it in
    # English rather than Python would only make it harder to see.
    bare = object.__new__(agent_module.GenpipeA1)
    bare.capabilities_enabled = True
    section = bare._capability_prompt()
    r.truthy("a section is produced when capabilities are on", section)
    r.contains("it names the calls", section, "check_run")
    r.contains("it shows how to write one", section, "<execute>")
    r.contains("it says the slash commands still exist", section, "/check")
    r.contains("and that the decision is the model's",
               section, "YOUR JUDGEMENT")
    r.contains("and that it is not obliged to use one",
               section, "not obliged")

    # THE RESTRAINT IS ABOUT IDENTICAL LOOKUPS, NOT ABOUT CAPABILITY NAMES.
    # "never call the same one twice" was the first wording and it was too
    # broad: comparing two runs is legitimately two check_run calls, and the
    # failed jobs in both is legitimately two inspect_jobs calls. The rule has
    # to name the thing that is actually wasteful -- the same call with the
    # same arguments -- and say the other case is ordinary.
    r.contains("the restraint names identical lookups",
               section, "IDENTICAL LOOKUP")
    r.contains("and says different arguments are fine",
               section, "DIFFERENT arguments")
    r.check("and does not forbid reusing a capability",
            "never call the same one twice" not in section)

    lowered = section.lower()
    # No phrase->action rules, in any of the shapes one would be written in.
    for phrase in ("if the user asks", "when the user asks", "if they ask about",
                   "when they ask about", "-> call", "→ call",
                   "use check_run when", "use diagnose_run when",
                   "always call", "you must call"):
        r.check(f"no rule of the form {phrase!r}", phrase not in lowered)

    bare.capabilities_enabled = False
    r.equal("and nothing at all when they are off",
            bare._capability_prompt(), "")

    # ------------------------------------------------------------------ #
    r.section("a lookup marks the proposal that follows it as a rebuild")
    # WHY THIS IS RECORDED AT ALL. "rerun Test_walltimefail without
    # override_walltime.ini" cannot be answered without looking that run up, so
    # the lookup is a reliable, prose-free signal that the proposal which
    # follows rebuilds something rather than being a fresh run -- and a rebuild
    # owes a declaration of what it changed (see agent._settle).
    #
    # It is the MODEL'S OWN ACTION, a capability call it chose to write. Nothing
    # here reads what the user typed, which is the whole reason it is usable as
    # a trigger.
    from genpipe import capabilities as _cap
    watcher = object.__new__(agent_module.GenpipeA1)
    watcher.capabilities_enabled = True
    watcher._runs_examined = set()

    # A registry that knows exactly one run. Only a name it knows counts as a
    # lookup of an existing run -- see below for why "all" and an invented name
    # must not, and note that the HANDLER still fails here (this stub has none
    # of the rest of a registry). That is deliberate: _run_capability turns a
    # failure into a note for the model, and the run was still asked about,
    # which is the fact being recorded.
    class _OneRun:
        def get(self, name):
            return {"name": name} if name == "poulet-0813" else None
    # `_registry`, not `registry`: the latter is a property (a data descriptor),
    # so an instance attribute of that name would never be consulted.
    watcher._registry = _OneRun()

    watcher._run_capability(_cap.get("show_run"), {"name": "poulet-0813"})
    r.check("the run the model asked about is remembered",
            "poulet-0813" in (watcher._runs_examined or ()),
            watcher._runs_examined)

    watcher._runs_examined = set()
    watcher._run_capability(_cap.get("list_runs"), {})
    r.equal("a lookup that names no run marks nothing",
            set(watcher._runs_examined or ()), set())

    # "all" IS A DOCUMENTED ARGUMENT to check_run and names no run. Recording it
    # marked the turn as a rebuild on the strength of somebody asking how their
    # runs were doing -- so "how is everything? then start a fresh dnaseq run"
    # put an undeclared-change warning on a first-ever proposal and refused its
    # first /approve, about a rebuild that never happened.
    r.contains("the capability really does document it",
               _cap.get("check_run").summary, '"all"')
    watcher._runs_examined = set()
    watcher._run_capability(_cap.get("check_run"), {"name": "all"})
    r.equal("so 'all' marks nothing", set(watcher._runs_examined or ()), set())

    # And a name no registry knows is not a run either -- a model that invented
    # one, or asked about a run that has been dropped.
    watcher._runs_examined = set()
    watcher._run_capability(_cap.get("check_run"), {"name": "no-such-run"})
    r.equal("nor does a name nothing knows",
            set(watcher._runs_examined or ()), set())

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
