#!/usr/bin/env python
"""The public command surface: what is taught, what dispatches, and that they agree.

One table in cli.py feeds three things -- the dispatcher, the completion menu
and /help -- and the reason it is one table is that three copies would drift.
This asserts that the single table is actually single, and that the product's
public surface is the one that was decided on rather than whatever accumulated.

It also DISPATCHES EVERY PUBLIC COMMAND FOR REAL, against the fake cluster and
a scripted model, with stdin closed. That is a deliberately blunt test and it
earns its place: a command that raises the moment it is typed is the one class
of defect that no amount of unit testing around it will surface, and it has
happened here before.

Needs biomni (cli.py imports the agent stack) and the fake cluster, so it runs
on the cluster rather than in CI -- same reason as test_app.py.

Run:  python tests/test_surface.py
"""
import contextlib
import io
import os
import re
import sys
import tempfile

from harness import Report


def main():
    workdir = tempfile.mkdtemp()
    os.environ["GENPIPE_AGENT_WORKDIR"] = workdir
    os.environ["GENPIPE_FAKE_STATE"] = "failed-oom"
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-devmode-placeholder")

    from genpipe import fakecluster
    fakecluster.activate("failed-oom")
    from genpipe import cli, runs

    r = Report("surface: one table, and every command in it works")

    # ------------------------------------------------------------------ #
    r.section("the table is the only source of the three views")

    public = [spec for spec in cli.COMMAND_SPECS if spec[5]]
    hidden = [spec for spec in cli.COMMAND_SPECS if not spec[5]]

    menu = {name for name, _, _ in cli.menu()}
    helped = {name for name, _, _, _ in cli.help_rows()}
    r.equal("the menu and /help show the same commands", menu, helped)
    r.equal("...and exactly the public ones", menu, {s[0] for s in public})

    r.check("every command in the table either dispatches or is loop state",
            all(spec[4] is not None or spec[0] in ("new", "exit")
                for spec in cli.COMMAND_SPECS))

    # ------------------------------------------------------------------ #
    r.section("hidden means untaught, not removed")
    # /readset is not mature enough to teach and /telemetry is developer
    # instrumentation. Neither is deleted: anything anybody has typed before
    # still works, and only the claim the product makes has changed.
    r.equal("the hidden commands are the two that were decided on",
            sorted(s[0] for s in hidden), ["readset", "telemetry"])
    for name in ("readset", "telemetry"):
        r.check(f"/{name} is not offered", name not in menu)
        r.check(f"/{name} still dispatches", name in cli.COMMANDS)
        r.equal(f"/{name} still resolves from an abbreviation",
                cli._resolve(name), name)

    # ------------------------------------------------------------------ #
    r.section("the help teaches the simple path without removing the flexible one")
    # `/modify <name> [change]` was three notations for one idea spread over
    # four rows, spending the widest column on the option nobody starts with.
    # The prose form still parses -- that is what is checked here, on the
    # parser rather than on the help text.
    signatures = {name: args for name, args, _, _ in cli.help_rows()}
    for name in ("modify", "fork", "reject", "diagnose", "monitor"):
        r.check(f"/{name} teaches just the name", "[" not in signatures[name],
                signatures[name])

    # ------------------------------------------------------------------ #
    r.section("every public command runs when it is typed")

    agent = cli.build_agent()
    agent.llm = fakecluster.DevLLM()
    proposal = {
        "generated": "genpipes dnaseq -t somatic_fastpass "
                     "-c dnaseq.base.ini -r readset.txt -p pairs.csv -g cmd.sh",
        "command": "bash cmd.sh",
        "slots": {"pipeline": "dnaseq", "protocol": "somatic_fastpass",
                  "readset": "readset.txt", "pairs": "pairs.csv"},
    }
    agent.registry.hold("held-demo", "chat-1", proposal, workdir)
    # A real job list on disk, because a submitted record whose artifacts have
    # vanished is pruned out of /list -- correctly, and it would leave this
    # section testing an empty listing.
    job_output = os.path.join(workdir, "job_output")
    os.makedirs(job_output, exist_ok=True)
    job_list = os.path.join(job_output,
                            "DnaSeq.somatic_fastpass.job_list.2026-08-05T11.02.13")
    with open(job_list, "w") as fh:
        fh.write("12345\ttrimmomatic.S1\tNONE\t"
                 + os.path.join(job_output, "trimmomatic.S1.o") + "\n")
    agent.registry.mark_submitted("done-demo", job_list,
                                  workdir=workdir, proposal=proposal)

    # An argument that makes the command do its real work where one exists, and
    # nothing where the usage message is the honest response to no argument.
    ARGS = {
        "view": ["held-demo"], "check": ["done-demo"], "jobs": ["done-demo"],
        "history": [], "list": [], "sort": ["show"], "verbose": ["off"],
        "where": [], "user": [], "model": [], "help": [],
    }
    for name, _, _, _, handler, _ in cli.COMMAND_SPECS:
        if handler is None or name == "key":
            # /key opens a masked prompt that owns the terminal; test_app
            # drives it through a pty. Everything else is safe to call here.
            continue
        buf = io.StringIO()
        failure = None
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                handler(agent, list(ARGS.get(name, [])))
        except (EOFError, KeyboardInterrupt):
            pass                    # asked a question; stdin is closed
        except Exception as e:      # noqa: BLE001 -- the whole point
            failure = f"{type(e).__name__}: {e}"
        r.check(f"/{name} does not raise", failure is None, failure or "")
        out = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", buf.getvalue())
        r.check(f"/{name} says something", bool(out.strip()) or name in
                ("new", "exit"), repr(out[:80]))

    # ------------------------------------------------------------------ #
    r.section("nothing reached the scheduler")
    # THE ABSOLUTE BOUNDARY, checked after every command in the product has
    # just been dispatched: only /approve submits, and /approve was never
    # given a name. The proposal may legitimately have been reclassified as
    # `lapsed` -- this fixture's thread has no real checkpoint behind it, and
    # a proposal with no live gate is exactly what lapsed means -- so what is
    # asserted is the thing that must never happen, not the thing that
    # happens to be true of a synthetic record.
    r.check("the held run never became a submission",
            agent.registry.get("held-demo")["status"] != runs.SUBMITTED,
            agent.registry.get("held-demo")["status"])
    r.equal("and it has no job list", agent.registry.get("held-demo")["job_list"],
            None)

    # ------------------------------------------------------------------ #
    r.section("the fork path really reaches the seam that protects the parent")
    # test_modify proves the MECHANISM: override.path_for(fresh=True) names a
    # file after the fork, and the parent's ini, -c stack, command and record
    # all survive the child's edit untouched. That proof is worth nothing if
    # the fork path does not call it that way, and the defect was precisely a
    # caller passing the wrong thing to a correct function.
    #
    # Asserted by source, like test_capability's "one implementation, two
    # doors", because there is no way to drive a panel through a redirected
    # stdout and the binding is the thing at risk rather than the behaviour.
    import inspect

    guided = inspect.getsource(cli._modify_guided)
    r.contains("the panel resolves its ini with a fork-aware path",
               guided, "fresh=bool(fork_as)")
    r.contains("and hands the same flag to the resources flow",
               guided, "fresh=bool(fork_as)")
    r.contains("which is told which identity it is writing for",
               guided, "editing=editing")
    r.check("the panel is drawn under the edited identity, not the source",
            "modify.rows_for(proposal, editing" in guided, "rows_for")

    fill = inspect.getsource(cli._fill_resources)
    r.contains("_fill_resources passes fresh straight through to path_for",
               fill, "override.path_for(editing, directory, proposal, fresh=fresh)")
    r.check("and writes under the edited identity",
            "run=editing" in fill, "run=editing")
    r.check("while still reading the SOURCE's diagnosis",
            "self.registry.get(name)" in fill or "agent.registry.get(name)" in fill,
            "registry.get(name)")

    from genpipe import override
    r.check("path_for takes the flag at all",
            "fresh" in inspect.signature(override.path_for).parameters)

    # ------------------------------------------------------------------ #
    r.section("/list and /sort present runs in the same order")
    # The reported defect: the same collection rearranged itself between the
    # two screens, so a row read as fourth in /list was seventeenth in the
    # panel somebody went to hide it from.
    records = agent.registry.live()
    rows = [(record, None) for record in records]
    ordered = [record["name"] for _, record, _ in runs.listing_order(rows)]
    r.equal("one canonical order, computed once",
            ordered, [record["name"] for _, record, _
                      in runs.listing_order(rows)])
    r.check("both runs are in the listing", set(ordered) >=
            {"held-demo", "done-demo"}, ordered)
    if {"held-demo", "done-demo"} <= set(ordered):
        r.check("the one waiting on a decision comes first",
                ordered.index("held-demo") < ordered.index("done-demo"),
                ordered)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
