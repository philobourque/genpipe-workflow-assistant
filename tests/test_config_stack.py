"""The -c stack: which ini is which, what order they are in, and whether a
change anybody asked for actually landed.

FOUR REPORTED DEFECTS, ONE FLAG. `-c` is the only GenPipes flag that is plural
and the only one whose ORDER decides what the run does, and every bug here came
from something treating it as neither:

  the parse       a regex read only the basenames of path-qualified inis, and
                  its `or` meant a bare relative ini was dropped from the
                  proposal entirely. `override_walltime.ini` was on the command
                  and not in the record.
  identity        the panel matched inis by basename, so one file spelled two
                  ways was two inis -- and two different files sharing a name
                  were one.
  order           membership was editable and precedence was not, and a reorder
                  produced an empty diff, so it reached the model as silence.
  verification    a change the model was asked to make and did not make had
                  nothing anywhere to check it against.

Stdlib only: gate, modify and intake are all free of biomni so this runs in CI.
"""
import os
import shutil
import tempfile

from harness import Report

from genpipe import gate
from genpipe import intake
from genpipe import modify


# The command from the report, in the shape the model actually writes it: four
# inis under an unexpanded $GENPIPES_INIS and one written plainly beside the run.
COMMAND = (
    "genpipes dnaseq -t somatic_fastpass -r readset.tsv -p pairs.csv "
    "-c $GENPIPES_INIS/dnaseq/dnaseq.base.ini "
    "$GENPIPES_INIS/dnaseq/dnaseq.cancer.ini "
    "$GENPIPES_INIS/common_ini/rorqual.ini "
    "$GENPIPES_INIS/dnaseq/cit.ini "
    "override_walltime.ini "
    "-o out -g cmd.sh"
)

STACK = ["$GENPIPES_INIS/dnaseq/dnaseq.base.ini",
         "$GENPIPES_INIS/dnaseq/dnaseq.cancer.ini",
         "$GENPIPES_INIS/common_ini/rorqual.ini",
         "$GENPIPES_INIS/dnaseq/cit.ini",
         "override_walltime.ini"]


def proposal(inis=None, **slots):
    values = {"pipeline": "dnaseq", "protocol": "somatic_fastpass",
              "inis": list(STACK if inis is None else inis)}
    values.update(slots)
    return {"slots": values, "generated": COMMAND}


def main():
    r = Report("The -c stack")

    # ==================================================================
    r.section("reading -c off the command")
    got = gate.flag_values(COMMAND, "-c")
    r.equal("every ini, in order, exactly as written", got, STACK)
    r.check("the bare relative ini is not dropped",
            "override_walltime.ini" in got, got)
    r.check("and the qualified ones keep their paths",
            all("/" in x for x in got[:4]), got)

    p = gate.build_proposal([], "#!BASH\n" + COMMAND)
    r.equal("the proposal records the same thing", p["slots"]["inis"], STACK)

    r.equal("the long form is read too",
            gate.flag_values("genpipes x --config a.ini b.ini -g y.sh", "-c"),
            ["a.ini", "b.ini"])
    r.equal("and --config=a.ini",
            gate.flag_values("genpipes x --config=a.ini -g y.sh", "-c"),
            ["a.ini"])
    r.equal("-c twice is one stack, joined in order",
            gate.flag_values("genpipes x -c a.ini -c b.ini -g y.sh", "-c"),
            ["a.ini", "b.ini"])
    r.equal("a flag ends the values",
            gate.flag_values("genpipes x -c a.ini -s 1-5", "-c"), ["a.ini"])
    r.equal("and so does the end of the line, not the next line's first word",
            gate.flag_values("genpipes x -c a.ini\necho done", "-c"), ["a.ini"])
    r.equal("a redirection is not an ini",
            gate.flag_values("genpipes x -c a.ini 2>&1", "-c"), ["a.ini"])
    r.equal("no -c at all is an empty stack, not a guess",
            gate.flag_values("genpipes x -r r.tsv", "-c"), [])
    # A shell separator ends the genpipes call, so what follows is not an ini.
    # build_proposal usually cuts at one before calling here -- but only when
    # there IS a genpipes call to cut to, and its documented fallback for a
    # proposal whose generation was never captured passes the whole block.
    for tail in ("&& bash cmd.sh", "; echo done", "| tee log", "|| exit 1"):
        r.equal(f"the command stops at {tail.split()[0]!r}",
                gate.flag_values(f"genpipes dnaseq -c a.ini {tail}", "-c"),
                ["a.ini"])

    # ==================================================================
    r.section("which ini is which")
    work = tempfile.mkdtemp(prefix="genpipe-inis-")
    try:
        local = os.path.join(work, "override_walltime.ini")
        open(local, "w").write("[step]\n")
        os.mkdir(os.path.join(work, "sub"))
        twin = os.path.join(work, "sub", "override_walltime.ini")
        open(twin, "w").write("[step]\n")

        stack = ["$GENPIPES_INIS/dnaseq/cit.ini", "override_walltime.ini"]
        r.equal("a bare name finds the file beside the run",
                modify.locate("override_walltime.ini", stack, work), (1,))
        r.equal("so does its absolute path",
                modify.locate(local, stack, work), (1,))
        r.equal("and so does a relative path through the workdir",
                modify.locate("./override_walltime.ini", stack, work), (1,))

        # THE HALF THAT BASENAME IDENTITY GOT WRONG. Same name, different file.
        r.equal("a same-named file in another directory is NOT the same ini",
                modify.locate(twin, stack, work), ())
        r.equal("nor is a same-named file under an install path",
                modify.locate("/some/custom/location/cit.ini", stack, work), ())
        r.equal("two unresolvable qualified paths stay distinct",
                modify.locate("$OTHER/cit.ini", stack, work), ())
        r.equal("while a bare name still binds to the qualified one",
                modify.locate("cit.ini", stack, work), (0,))

        r.equal("a bare name matching two entries is ambiguous, not a guess",
                len(modify.locate("cit.ini", ["a/cit.ini", "b/cit.ini"])), 2)
        amb = {"slots": {"inis": ["a/cit.ini", "b/cit.ini"]}}
        r.equal("so toggling an ambiguous name changes nothing",
                modify.toggle_config(amb, {}, "cit.ini"),
                ["a/cit.ini", "b/cit.ini"])
        r.equal("and neither does moving it",
                modify.move_config(amb, {}, "cit.ini", 1),
                ["a/cit.ini", "b/cit.ini"])

        r.section("removal, by each of the three spellings")
        for label, spelling in (("basename", "override_walltime.ini"),
                                ("relative path", "./override_walltime.ini"),
                                ("absolute path", local)):
            after = modify.toggle_config(proposal(), {}, spelling, workdir=work)
            r.equal(f"removed when named by its {label}", after, STACK[:4])
        # A different file, so toggling it is an ADD -- and crucially it must
        # not take the same-named ini already on the stack off with it.
        both = modify.toggle_config(proposal(), {}, twin, workdir=work)
        r.equal("a same-named file elsewhere is added, not swapped in",
                both, STACK + [twin])
        r.check("and the ini it shares a name with is still there",
                "override_walltime.ini" in both, both)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # ==================================================================
    r.section("display labels are short but never ambiguous")
    labels = modify.labels_for(STACK + ["/home/p/proj/override_walltime.ini"])
    r.equal("a qualified ini shows as its basename",
            labels["$GENPIPES_INIS/dnaseq/cit.ini"], "cit.ini")
    r.check("a long absolute path does not show in full",
            len(labels["/home/p/proj/override_walltime.ini"]) < 30,
            labels["/home/p/proj/override_walltime.ini"])
    clash = modify.labels_for(["$GENPIPES_INIS/common_ini/rorqual.ini",
                               "/home/p/proj/rorqual.ini"])
    r.equal("two rorqual.ini are told apart", len(set(clash.values())), 2)
    for path, label in clash.items():
        r.check(f"and each says enough to know which ({label})",
                label != "rorqual.ini", label)

    # ==================================================================
    r.section("order is editable, and independently of membership")
    base = proposal(["a.ini", "b.ini", "c.ini"])
    r.equal("an ini moves later", modify.move_config(base, {}, "a.ini", 1),
            ["b.ini", "a.ini", "c.ini"])
    r.equal("and earlier", modify.move_config(base, {}, "c.ini", -1),
            ["a.ini", "c.ini", "b.ini"])
    r.equal("the top of the stack does not wrap to the bottom",
            modify.move_config(base, {}, "a.ini", -1),
            ["a.ini", "b.ini", "c.ini"])
    r.equal("nor the bottom to the top",
            modify.move_config(base, {}, "c.ini", 1),
            ["a.ini", "b.ini", "c.ini"])
    r.equal("an ini that is not on the stack does not move onto it",
            modify.move_config(base, {}, "z.ini", -1),
            ["a.ini", "b.ini", "c.ini"])
    once = modify.move_config(base, {}, "a.ini", 1)
    r.equal("pressing twice moves it twice",
            modify.move_config(base, {"config": once}, "a.ini", 1),
            ["b.ini", "c.ini", "a.ini"])

    r.section("a reorder is a real change, and a removal is not a reorder")
    r.truthy("swapping two inis is a change",
             modify.reordered(base, {"config": ["b.ini", "a.ini", "c.ini"]}))
    r.check("dropping one is not, though the others shift along",
            not modify.reordered(base, {"config": ["a.ini", "c.ini"]}))
    r.check("nor is appending one",
            not modify.reordered(base, {"config": ["a.ini", "b.ini", "c.ini",
                                                   "d.ini"]}))
    r.truthy("but dropping AND resequencing is",
             modify.reordered(base, {"config": ["c.ini", "a.ini"]}))
    r.equal("membership still reports only membership",
            modify.config_delta(base, {"config": ["c.ini", "a.ini"]}),
            ([], ["b.ini"]))

    r.section("and the model is told about it")
    said = modify.sentence(base, {"config": ["b.ini", "a.ini", "c.ini"]})
    r.truthy("a pure reorder produces a sentence at all", said)
    r.contains("naming the exact sequence", said, "b.ini a.ini c.ini")
    r.contains("and saying why order matters", said, "later inis overrule")
    r.truthy("so does a reorder combined with a removal",
             modify.sentence(base, {"config": ["c.ini", "a.ini"]}))
    r.equal("an unchanged stack still says nothing",
            modify.sentence(base, {"config": ["a.ini", "b.ini", "c.ini"]}), "")

    # NO POLICY ABOUT WHAT THE ORDER SHOULD BE. Asserted as a property rather
    # than by grepping for forbidden words: the move is PURELY POSITIONAL, so
    # renaming every ini cannot change what it does. A table that knew rorqual
    # belonged before cancer, or that an override belonged last, would fail this
    # the moment the names stopped being the ones it knew.
    r.section("and it holds no opinion about what the right order is")
    real = ["dnaseq.base.ini", "dnaseq.cancer.ini", "rorqual.ini", "cit.ini",
            "override_walltime.ini"]
    anon = ["q1.ini", "q2.ini", "q3.ini", "q4.ini", "q5.ini"]
    for at in range(len(real)):
        for by in (-1, 1):
            moved_real = modify.move_config(proposal(real), {}, real[at], by)
            moved_anon = modify.move_config(proposal(anon), {}, anon[at], by)
            r.equal(f"moving {at} by {by:+d} is positional, not nominal",
                    [real.index(x) for x in moved_real],
                    [anon.index(x) for x in moved_anon])

    # ==================================================================
    r.section("three states in the picker")
    opts = modify.options_for("config", proposal(),
                              candidates={"config": ["spare.ini"]},
                              pending={"config": STACK[:4]},
                              removed=["override_walltime.ini"])
    marks = {o.label.split()[-1]: o.label[0] for o in opts}
    r.equal("an ini on the stack is ticked", marks.get("cit.ini"),
            modify.ON_MARK)
    r.equal("one taken off this pass is crossed",
            marks.get("override_walltime.ini"), modify.OFF_MARK)
    r.equal("and one merely available is left blank",
            marks.get("spare.ini"), modify.FREE_MARK)

    # THE DESCRIPTION SAYS WHAT THE ROW IS. What the keys do to it belongs on
    # the highlighted row alone, and the renderer puts it there -- see
    # display.modify_panel. Carried on every option instead, `· [ ] reorders`
    # was the same sentence four times over, next to inis those keys cannot
    # move.
    notes = {o.label.split()[-1]: o.description for o in opts}
    r.equal("an included ini describes its state and nothing else",
            notes.get("cit.ini"), "on the stack")
    r.equal("a removed one says how to bring it back",
            notes.get("override_walltime.ini"), "removed · enter restores")
    r.check("and no static description advertises the reorder keys",
            not any("[" in (o.description or "") for o in opts),
            [o.description for o in opts])

    # THE INCONSISTENCY THAT WAS REPORTED. An ini added and then removed in the
    # same pass was never in the original stack, so the old "in the original and
    # not in the pending stack" inference showed it blank while cit.ini in the
    # identical situation showed a cross. One keystroke, two renderings.
    opts = modify.options_for("config", proposal(),
                              candidates={"config": ["spare.ini"]},
                              pending={"config": STACK},
                              removed=["spare.ini"])
    marks = {o.label.split()[-1]: o.label[0] for o in opts}
    r.equal("an ini added and removed in one pass is crossed too",
            marks.get("spare.ini"), "✗")
    r.check("every option still carries the exact path as its value",
            any(o.value == "override_walltime.ini" for o in opts),
            [o.value for o in opts])
    r.check("including the qualified ones, unshortened",
            any(o.value == "$GENPIPES_INIS/dnaseq/cit.ini" for o in opts),
            [o.value for o in opts])

    # LABELS ARE MEASURED OVER WHAT IS DRAWN, not over everything considered.
    # slots.expected_inis knows dnaseq.cancer.ini by its bare name while the
    # command carries $GENPIPES_INIS/dnaseq/dnaseq.cancer.ini. Both used to go
    # into the label pool, collide on the basename and grow a parent directory
    # each -- so one row on the stack showed a path while its neighbours showed
    # names, disambiguating it from a row that had been dropped as a duplicate
    # and was not on screen at all.
    shown = [o.label[2:] for o in modify.options_for(
        "config", proposal(), candidates={"config": []},
        pending=None, removed=[])]
    r.equal("no row grows a directory to differ from one that is not shown",
            [x for x in shown if "/" in x], [])
    r.check("every ini on the stack reads as its name", 
            {"dnaseq.cancer.ini", "cit.ini", "override_walltime.ini"}
            <= set(shown), shown)
    r.equal("and the rows are still one per ini, not one per spelling",
            len(shown), len(set(shown)))

    # ==================================================================
    r.section("past run configs are one door, not a growing list")
    # GenPipes writes a resolved config beside every command it generates, so a
    # working directory gains one per generation for ever. They were candidates
    # like any other ini and filled the panel; then they were capped at the two
    # newest, which fixed the length by making the rest unreachable. The door
    # costs one row after two generations and one row after two hundred.
    opts = modify.options_for("config", proposal(),
                              candidates={"config": ["spare.ini"]})
    door = [o for o in opts if o.value == modify.PAST_CONFIGS]
    r.equal("there is exactly one", len(door), 1)
    r.equal("and it is last, under the inis", opts[-1].value,
            modify.PAST_CONFIGS)
    r.contains("saying what is behind it", door[0].label, "Past run configs")
    r.check("its value cannot collide with any ini anybody has",
            "\x00" in modify.PAST_CONFIGS and "\x00" in modify.SEARCH_TRACKED)
    r.check("and it carries no state marker, so it is not a config",
            not door[0].label.startswith((modify.ON_MARK, modify.OFF_MARK)),
            door[0].label)

    r.section("a past config joins the stack one of two explicit ways")
    # Both are offered plainly and neither is marked as the right one: a
    # resolved trace already holds everything its run was given, so laying it
    # ON a stack and starting FROM it are genuinely different runs, and which
    # one somebody means is not something this code can know.
    trace = "/proj/DnaSeq.somatic_fastpass.2026-08-05T11.02.13.config.trace.ini"
    base = proposal(["a.ini", "b.ini"])
    r.equal("used as the stack, it replaces what was there",
            modify.use_config(base, {}, trace), [trace])
    r.equal("added, it is one more ordered entry",
            modify.toggle_config(base, {}, trace), ["a.ini", "b.ini", trace])
    r.equal("and either way more inis still layer after it in the usual way",
            modify.toggle_config(base, {"config": [trace]}, "c.ini"),
            [trace, "c.ini"])
    r.equal("a stack of one still reorders like any other",
            modify.move_config(base, {"config": [trace, "c.ini"]}, "c.ini", -1),
            ["c.ini", trace])

    r.section("what a past config is described as")
    meta = {"path": trace, "pipeline": "dnaseq", "protocol": "somatic_fastpass",
            "stamp": "2026-08-05T11.02.13", "script": "cit_rerun.sh"}
    label, note = modify.trace_row(meta)
    r.equal("the label is what it is, not what it is called",
            label, "dnaseq · somatic_fastpass · 2026-08-05 11:02")
    r.check("so the forty-character filename is not the row", 
            "config.trace.ini" not in label, label)
    r.equal("with no tracked run to point at, it names the script it wrote",
            note, "generated cit_rerun.sh")
    _, owned = modify.trace_row(meta, {"name": "poulet-0805",
                                       "status": "submitted"})
    r.equal("and with exactly one, the run and its state", owned,
            "from poulet-0805 · submitted")
    # A run that FAILED is described, not withheld: reusing the config of a run
    # that went wrong is a real thing to want.
    _, failed = modify.trace_row(meta, {"name": "poulet-0805",
                                        "status": "submit_failed"})
    r.contains("including one that failed", failed, "submit failed")

    r.equal("a timestamp is made readable", modify.pretty_stamp(
        "2026-08-05T11.02.13"), "2026-08-05 11:02")
    r.equal("and one that is not that shape is left exactly as found",
            modify.pretty_stamp("whenever"), "whenever")
    bare_label, bare_note = modify.trace_row({"path": "/x/odd.config.trace.ini"})
    r.equal("a trace that says nothing about itself falls back to its name",
            bare_label, "odd.config.trace.ini")
    r.equal("and claims no origin", bare_note, "written by GenPipes")

    r.section("the source directory appears only when it varies")
    # Decided from the directories that PRODUCED results, not the ones that
    # were searched. Widening to three tracked runs that turn out to hold no
    # traces still yields one directory's worth, and a column repeating that
    # one path beside every row says nothing -- while two directories' worth
    # is unreadable without it.
    #
    # Asserted against the source because the decision lives in cli.py, which
    # imports biomni and so cannot be driven from this suite. What matters is
    # the QUANTITY it keys off.
    import os as _os
    _cli = open(_os.path.join(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))), "genpipe", "cli.py")).read()
    view = _cli[_cli.index("def _past_configs"):]
    view = view[:view.index("\ndef ")]
    r.contains("the set of directories results came from is what is measured",
               view, "homes = {os.path.dirname(trace[\"path\"]) for trace")
    r.contains("and the path is shown only when there is more than one",
               view, "if len(homes) > 1:")
    r.check("never from the number of directories searched",
            "len(places) > 1" not in view, view)

    r.section("and none of it is remembered or crawled")
    # THE CONSTRAINT THIS FEATURE EXISTS UNDER. A directory gains a trace every
    # time a command is generated, so anything that indexed, cached or persisted
    # them would be a structure that only grows -- and the reason the primary
    # picker had to stop listing them in the first place.
    #
    # Asserted against the source because the property is an ABSENCE, and an
    # absence has no call to make. intake.traces is one listdir and a bounded
    # read per file; there is nothing here to test by driving it that would fail
    # if a module-level cache appeared next to it.
    # The CODE, with the prose taken out: these functions describe what they
    # deliberately do not do, and a substring test over a docstring saying
    # "uncached" reads it as a cache.
    import ast as _ast, inspect as _inspect
    def _code(fn):
        tree = _ast.parse(_inspect.getsource(fn).lstrip())
        for node in _ast.walk(tree):
            if (isinstance(node, (_ast.FunctionDef, _ast.Module))
                    and node.body and isinstance(node.body[0], _ast.Expr)
                    and isinstance(node.body[0].value, _ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:] or [_ast.Pass()]
        return _ast.unparse(_ast.fix_missing_locations(tree))
    scan = _code(intake.traces) + _code(intake.read_trace)
    for crawl in ("os.walk", "glob", "rglob", "**", "SCAN_DEPTH", "discover("):
        r.check(f"no {crawl!r} -- one directory, never a walk",
                crawl not in scan, scan[:200])
    for kept in ("lru_cache", "cache", "_TRACES", "global "):
        r.check(f"no {kept!r} -- scanned on demand, never kept",
                kept not in scan)
    r.check("and the module holds no trace collection of its own",
            not [n for n in dir(intake)
                 if "trace" in n.lower() and isinstance(
                     getattr(intake, n), (list, dict, set))],
            [n for n in dir(intake) if "trace" in n.lower()])

    return r.finish()


if __name__ == "__main__":
    raise SystemExit(main())
