"""A change the model declared, and whether the command it wrote actually
makes it.

THE RUN THIS SUITE IS ABOUT. Somebody typed:

    I want you to rerun Test_walltimefail, while removing override_walltime.ini

and the command that came back still carried override_walltime.ini. Nothing
caught it, and the run was submitted. Two independent failures compounded: the
proposal's `-c` parse had dropped that ini entirely (test_config_stack covers
the parse), and nothing had been told the change was wanted, so
modify.compare's IGNORED verdict was unreachable and the box was drawn with no
mark on it.

THE BOUNDARY THIS ENFORCES, and the reason none of it is a phrase parser:

    the model    reads the sentence, decides it means "take that ini off the
                 -c stack", and SAYS SO in the call that creates the proposal:
                 propose_submission(script, changes=[...])
    this code    checks the command it generated against that declaration

Nothing here reads anybody's English. The inputs are a structured declaration
the model authored and a command the model wrote; the output is set membership
and string equality. A test that passed because "remove" appeared in a sentence
would be testing the thing this architecture exists to not have.
"""
from harness import Report

from genpipe import gate
from genpipe import modify


BEFORE = ["$GENPIPES_INIS/dnaseq/dnaseq.base.ini",
          "$GENPIPES_INIS/dnaseq/cit.ini",
          "override_walltime.ini"]


def generated(inis):
    return ("genpipes dnaseq -t somatic_fastpass -r readset.tsv -p pairs.csv "
            "-c " + " ".join(inis) + " -o out -g cmd.sh")


def block(inis):
    return f"<execute>\n#!BASH\n{generated(inis)}\n</execute>"


class Msg:
    def __init__(self, content):
        self.content = content


def main():
    r = Report("Declared changes, and their realisation")

    # ==================================================================
    r.section("what the model may declare")
    call = ('propose_submission("cmd.sh", changes=[\n'
            '    {"field": "config", "operation": "remove",\n'
            '     "value": "override_walltime.ini"}])')
    declared = gate.declared_changes(call)
    r.equal("field, operation and value stay three separate things", declared,
            [{"field": "config", "operation": "remove",
              "value": "override_walltime.ini"}])
    r.equal("the submission is still recognised as one",
            gate.is_submission(call), True)
    r.equal("and the script it names is unchanged",
            gate.proposed_script(call), "cmd.sh")

    r.section("the three answers that are not the same answer")
    r.equal("no changes= at all is UNDECLARED, not 'nothing changed'",
            gate.declared_changes('propose_submission("cmd.sh")'), None)
    r.equal("an empty list is a deliberate 'I changed nothing'",
            gate.declared_changes('propose_submission("cmd.sh", changes=[])'),
            [])
    r.equal("an unknown field is refused rather than dropped",
            gate.declared_changes(
                'propose_submission("c.sh", changes=[{"field": "genome", '
                '"operation": "set", "value": "hg38"}])'),
            gate.MALFORMED)
    r.equal("so is an operation the field does not have",
            gate.declared_changes(
                'propose_submission("c.sh", changes=[{"field": "steps", '
                '"operation": "remove", "value": "1-4"}])'),
            gate.MALFORMED)
    r.equal("and a well-formed entry beside a broken one does not survive alone",
            gate.declared_changes(
                'propose_submission("c.sh", changes=[{"field": "steps", '
                '"operation": "set", "value": "1-4"}, {"nonsense": 1}])'),
            gate.MALFORMED)

    r.section("and it is read from the model's code, never from prose")
    # The same rule ask() and the capabilities follow. A model narrating its own
    # work must not be able to put a claim on the record.
    r.equal("a declaration inside a print is not a declaration",
            gate.declared_changes(
                'print("propose_submission(x, changes=[{...}])")'), None)
    r.equal("and a sentence describing one is not either",
            gate.declared_changes(
                "I will remove override_walltime.ini from the -c stack"), None)

    # ==================================================================
    r.section("THE REPORTED BUG: the declared removal did not happen")
    # The model said it was taking the ini off. The command it generated still
    # has it. This is the case that used to reach `submitted` unremarked.
    still_there = gate.build_proposal([Msg(block(BEFORE))], call)
    r.equal("the proposal carries what the model claimed",
            still_there["declared"], declared)
    r.equal("the command still carries the ini", still_there["slots"]["inis"],
            BEFORE)
    r.equal("so the change is reported as NOT applied",
            modify.realized(still_there["declared"], still_there),
            {"config": modify.IGNORED})

    honoured = gate.build_proposal([Msg(block(BEFORE[:2]))], call)
    r.equal("and when the ini really is gone, it is applied",
            modify.realized(honoured["declared"], honoured),
            {"config": modify.APPLIED})

    r.section("the ini is matched however either side spells it")
    for spelling in ("override_walltime.ini", "./override_walltime.ini"):
        said = [{"field": "config", "operation": "remove", "value": spelling}]
        left = {"slots": {"inis": BEFORE}}
        r.equal(f"still present, declared as {spelling!r}",
                modify.realized(said, left), {"config": modify.IGNORED})

    # AMBIGUITY FAILS CLOSED. If two entries could be the ini that was supposed
    # to come off, it did not come off.
    r.equal("an ambiguous match counts as still present",
            modify.realized(
                [{"field": "config", "operation": "remove", "value": "cit.ini"}],
                {"slots": {"inis": ["a/cit.ini", "b/cit.ini"]}}),
            {"config": modify.IGNORED})

    r.section("adding, and several claims about one flag")
    two = [{"field": "config", "operation": "remove", "value": "cit.ini"},
           {"field": "config", "operation": "add", "value": "dnaseq.exome.ini"}]
    r.equal("both honoured is applied",
            modify.realized(two, {"slots": {"inis": ["dnaseq.exome.ini"]}}),
            {"config": modify.APPLIED})
    r.equal("one honoured and one dropped is not",
            modify.realized(two, {"slots": {"inis": ["cit.ini",
                                                     "dnaseq.exome.ini"]}}),
            {"config": modify.IGNORED})

    r.section("a declared order")
    seq = [{"field": "config", "operation": "reorder",
            "value": ["b.ini", "a.ini"]}]
    r.equal("the exact sequence is applied",
            modify.realized(seq, {"slots": {"inis": ["b.ini", "a.ini"]}}),
            {"config": modify.APPLIED})
    r.equal("the same inis in the other order are not",
            modify.realized(seq, {"slots": {"inis": ["a.ini", "b.ini"]}}),
            {"config": modify.IGNORED})
    r.equal("and neither is a stack of a different length",
            modify.realized(seq, {"slots": {"inis": ["b.ini", "a.ini",
                                                     "c.ini"]}}),
            {"config": modify.IGNORED})

    r.section("scalar rows")
    steps = [{"field": "steps", "operation": "set", "value": "1-4"}]
    r.equal("the flag has the declared value",
            modify.realized(steps, {"slots": {"steps": "1-4"}}),
            {"steps": modify.APPLIED})
    r.equal("it does not", modify.realized(steps, {"slots": {"steps": "1-5"}}),
            {"steps": modify.IGNORED})
    r.equal("and whitespace is not a difference",
            modify.realized(steps, {"slots": {"steps": " 1-4 "}}),
            {"steps": modify.APPLIED})

    # ==================================================================
    r.section("no baseline is not the same as no change")
    # A rerun lands under a NEW name, so there is no previous proposal to diff
    # against. compare() used to answer IGNORED for every requested row in that
    # situation -- which made /fork mark a panel full of applied changes red --
    # and the fix must not go the other way and mark them green either.
    after = {"slots": {"protocol": "atacseq", "steps": "1-4", "inis": []}}
    r.equal("with nothing to compare against, compare says nothing",
            modify.compare(None, after, ["protocol", "steps"]), {})
    r.equal("a resources change is still answered, having no flag to diff",
            modify.compare(None, after, ["resources"]),
            {"resources": modify.APPLIED})
    was = {"slots": {"protocol": "chipseq", "steps": "1-4", "inis": []}}
    r.equal("and with a baseline it still reports both ways",
            modify.compare(was, after, ["protocol", "steps"]),
            {"protocol": modify.APPLIED, "steps": modify.IGNORED})
    # Which is exactly why realized() exists: it needs no baseline.
    r.equal("realisation is checkable with no baseline at all",
            modify.realized([{"field": "protocol", "operation": "set",
                              "value": "atacseq"}], after),
            {"protocol": modify.APPLIED})

    # ==================================================================
    r.section("the panel declares the same way, so both paths are checked")
    # /modify and /fork build a change set by keystroke. It is restated in the
    # same schema and checked by the same function -- one verifier, not two.
    before = {"slots": {"inis": ["a.ini", "b.ini", "c.ini"], "steps": "1-5"}}
    said = modify.declaration(before, {"config": ["a.ini", "c.ini"],
                                       "steps": "1-4"})
    r.check("a removal picked in the panel becomes a declared removal",
            {"field": "config", "operation": "remove", "value": "b.ini"} in said,
            said)
    r.check("and a typed value becomes a declared set",
            {"field": "steps", "operation": "set", "value": "1-4"} in said, said)
    r.equal("neither name nor resources is declared, having nothing to check",
            modify.declaration(before, {"name": "x", "resources": "y.ini"}), [])
    r.equal("honoured", modify.realized(
        said, {"slots": {"inis": ["a.ini", "c.ini"], "steps": "1-4"}}),
        {"config": modify.APPLIED, "steps": modify.APPLIED})
    r.equal("and the model quietly keeping b.ini is caught",
            modify.realized(said, {"slots": {"inis": ["a.ini", "b.ini", "c.ini"],
                                             "steps": "1-4"}}),
            {"config": modify.IGNORED, "steps": modify.APPLIED})

    r.section("a reorder picked in the panel is declared as a sequence")
    spun = modify.declaration(before, {"config": ["b.ini", "a.ini", "c.ini"]})
    r.equal("the whole order is the claim",
            [e for e in spun if e["operation"] == "reorder"],
            [{"field": "config", "operation": "reorder",
              "value": ["b.ini", "a.ini", "c.ini"]}])
    r.equal("a stack the model put back the old way is caught",
            modify.realized(spun, {"slots": {"inis": ["a.ini", "b.ini",
                                                      "c.ini"]}}),
            {"config": modify.IGNORED})

    r.section("the shapes the seams actually pass around")
    # BOTH OF THESE WERE CRASHES, found in review and not by the suite, because
    # nothing here drove the guided-apply path end to end. They are asserted as
    # SHAPES rather than by re-driving that path, so the contract is checked
    # wherever the value is built rather than only where it happened to break.
    #
    # declaration() returns a LIST of three-key dicts. cli._rework wrapped it in
    # dict(), which reads a sequence of PAIRS -- so every in-place /modify of a
    # declarable row died with ValueError and lost the change set.
    made = modify.declaration({"slots": {"inis": ["a.ini"], "steps": "1-5"}},
                              {"steps": "1-4"})
    r.check("declaration returns a list", isinstance(made, list), type(made))
    r.check("of dicts with three keys each",
            all(isinstance(e, dict) and set(e) == {"field", "operation", "value"}
                for e in made), made)
    try:
        dict(made)
        crashed = None
    except Exception as e:                    # noqa: BLE001
        crashed = type(e).__name__
    r.equal("which dict() cannot swallow -- so no seam may try",
            crashed, "ValueError")
    r.check("list() is the conversion that works",
            list(made) == made, made)

    # A run record's `changed` is a MAPPING when the gate wrote verdicts and a
    # LIST of row names when cli._redraw wrote it for a change that cost no
    # model call. A reader that assumes one shape raises on the other.
    for shape in ({"config": modify.IGNORED}, ["resources"], None, {}):
        marks = shape
        ignored = ([row for row, v in marks.items() if v == modify.IGNORED]
                   if isinstance(marks, dict) else [])
        r.check(f"reading verdicts out of {type(shape).__name__} does not raise",
                isinstance(ignored, list), ignored)
    r.equal("and only the mapping form carries a verdict to act on",
            [row for row, v in {"config": modify.IGNORED}.items()
             if v == modify.IGNORED], ["config"])

    r.section("a declaration that could not be read verifies nothing")
    # gate.MALFORMED is a STRING, and a truthy one. Iterating it yields
    # characters, so an unguarded verifier would raise an AttributeError from
    # inside the gate -- turning a model's typo into a crash on the screen
    # somebody approves from. Nothing was verified, so nothing is claimed.
    r.equal("no verdicts at all", modify.realized(gate.MALFORMED, {"slots": {}}),
            {})
    r.equal("and nothing to say about it", modify.wording(gate.MALFORMED), {})
    r.equal("an absent declaration is the same",
            modify.realized(None, {"slots": {}}), {})
    r.equal("an entry with no operation is answered with silence, not APPLIED",
            modify.realized([{"field": "config"}], {"slots": {"inis": []}}), {})
    r.equal("and so is an operation the field does not have",
            modify.realized([{"field": "steps", "operation": "remove",
                              "value": "1-4"}], {"slots": {"steps": "1-5"}}), {})

    r.section("what the red row says you asked for")
    r.contains("a removal is described as one", modify.wording(declared)["config"],
               "override_walltime.ini off the -c stack")
    r.contains("and an order as the order", modify.wording(spun)["config"],
               "b.ini , a.ini , c.ini")
    r.check("with no sigils left in it",
            not modify.wording(declared)["config"].startswith("-"),
            modify.wording(declared)["config"])

    return r.finish()


if __name__ == "__main__":
    raise SystemExit(main())
