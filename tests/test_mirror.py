#!/usr/bin/env python
"""mirror.read(): tokenising a generated command back into its shape.

Regression coverage for the bug where an already-launched run's /modify
screen showed the whole command crammed under `protocol` -- flags like -c,
-r, -p, -s, -j and -g never recognised as flags at all. Root cause: a
multi-line command using backslash-newline continuations (very ordinary
shell formatting) had its newlines flattened to spaces WITHOUT removing the
backslash first, so shlex's POSIX quoting rules read `\\ ` as an escaped
space rather than a line break -- the token after it came out as `' -c'`,
leading space and all, which `_FLAG` never matches. A held run's freshly
generated single-line command never hits this path, which is why held and
launched runs appeared to be parsed differently when the actual difference
was just which SHAPE of command each one happened to be.

Stdlib only, like mirror.py itself.

Run:  python tests/test_mirror.py
"""
import sys

from harness import Report

from genpipe import mirror


# The exact shape from the bug report: `genpipes dnaseq`, one flag per line,
# each line ending in a backslash continuation, ending in `2>&1`.
CONTINUED = (
    "genpipes dnaseq -t somatic_fastpass \\\n"
    "  -c $GENPIPES_INIS/dnaseq/dnaseq.base.ini \\\n"
    "  -r $MUGQIC_INSTALL_HOME/testdata/dnaseq/readset.somatic_fastpass.txt \\\n"
    "  -p $MUGQIC_INSTALL_HOME/testdata/dnaseq/pairs.somatic_fastpass.csv \\\n"
    "  -s 1-23 \\\n"
    "  -j slurm \\\n"
    "  -g dnaseq_somatic_fastpass_cit.sh 2>&1"
)


def main():
    r = Report("mirror.read(): tokenising a generated command")

    # ------------------------------------------------------------------ #
    r.section("a single-line command -- the common, already-working case")

    m = mirror.read("genpipes rnaseq -t stringtie -s 1-5 -g cmd.sh",
                    name="rnaseq-stringtie-0804")
    by_row = {line.row: line for line in m.lines}
    r.equal("protocol reads only its own value",
            by_row["protocol"].values, ["stringtie"])
    r.equal("steps is its own row, not swallowed by protocol",
            by_row["steps"].values, ["1-5"])
    r.truthy("nothing named script leaked into protocol's values",
             "cmd.sh" not in by_row["protocol"].values)

    # ------------------------------------------------------------------ #
    r.section("backslash-continued multi-line command -- the bug")

    m = mirror.read(CONTINUED, name="Test_walltimefail")
    by_row = {line.row: line for line in m.lines}

    r.equal("protocol holds only the protocol name",
            by_row["protocol"].values, ["somatic_fastpass"])
    r.equal("steps is its own row", by_row["steps"].values, ["1-23"])
    r.equal("pairs is its own row",
            by_row["pairs"].values,
            ["$MUGQIC_INSTALL_HOME/testdata/dnaseq/pairs.somatic_fastpass.csv"])
    r.equal("readset is its own row",
            by_row["readset"].values,
            ["$MUGQIC_INSTALL_HOME/testdata/dnaseq/readset.somatic_fastpass.txt"])
    r.equal("config is its own row",
            by_row["config"].values,
            ["$GENPIPES_INIS/dnaseq/dnaseq.base.ini"])
    r.truthy("none of -c/-r/-p/-s/-j/-g's flags survive as literal text "
             "inside protocol's value",
             not any(v.startswith("-") for v in by_row["protocol"].values))

    scheduler = next(line for line in m.lines if line.flag == "-j")
    r.equal("the scheduler flag is read as its own line, not lost",
            scheduler.values, ["slurm"])
    script = next(line for line in m.lines if line.flag == "-g")
    r.equal("the script flag holds only the filename",
            script.values, ["dnaseq_somatic_fastpass_cit.sh"])

    # ------------------------------------------------------------------ #
    r.section("shell redirection is plumbing, not a GenPipes value")

    for suffix in (" 2>&1", " > out.log", " >> out.log", " 2> err.log"):
        m = mirror.read("genpipes rnaseq -t stringtie -g cmd.sh" + suffix)
        script = next(line for line in m.lines if line.flag == "-g")
        r.equal(f"{suffix!r} never becomes part of -g's value",
                script.values, ["cmd.sh"])

    # A redirection target that could be confused for a real value -- this is
    # the case that actually broke: 2>&1 sitting right after a real filename.
    m = mirror.read(CONTINUED)
    script = next(line for line in m.lines if line.flag == "-g")
    r.equal("2>&1 does not tag along after the script filename",
            script.values, ["dnaseq_somatic_fastpass_cit.sh"])

    # ------------------------------------------------------------------ #
    r.section("a command that still can't be trusted degrades to empty, "
              "not to a wrong-looking mirror")

    # A pathological case neither fix covers -- a flag-shaped token buried
    # inside another flag's own value, the general shape both fixes above
    # exist to catch even in forms nobody has hit yet. read() must refuse to
    # draw a mirror that puts a flag inside another row's value, rather than
    # show one that LOOKS like a real reconstruction and is not.
    poisoned = "genpipes rnaseq -t 'stringtie -s 1-5' -g cmd.sh"
    m = mirror.read(poisoned)
    r.check("an unreliable parse returns an empty Mirror, not a wrong one",
            not m)

    # ------------------------------------------------------------------ #
    r.section("read() vs from_slots(): the fallback this module documents")

    proposal = {
        "generated": "",  # never captured -- read() has nothing to tokenise
        "slots": {"pipeline": "rnaseq", "protocol": "stringtie",
                 "readset": "readset.tsv"},
        "missing": [],
    }
    r.check("no generated text means read() draws nothing",
            not mirror.read(proposal["generated"]))
    m = mirror.from_slots(proposal, name="fallback-run")
    by_row = {line.row: line for line in m.lines}
    r.equal("from_slots still recovers what the slots parser found",
            by_row["protocol"].values, ["stringtie"])

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
