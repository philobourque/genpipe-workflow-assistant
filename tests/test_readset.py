#!/usr/bin/env python
"""Building and checking a readset file from its structure alone.

Every test here runs against filenames and schemas. None of them opens a FASTQ,
and that is the property under test as much as any assertion: the assistant does
not need to read anybody's data in order to act on it, and this is the module
where that could most easily have stopped being true.

The synthetic rows are the demonstration. Anything written against

    Sample  Readset  Library  RunType      FASTQ1           FASTQ2
    fake_A  rs001    lib01    PAIRED_END   /fake/r1.fq.gz   /fake/r2.fq.gz

works unchanged on the real thing, because the logic depends on where a column
is and what is allowed in it -- not on what is inside.

Run:  python tests/test_readset.py
"""
import os
import sys
import tempfile

from harness import Report

from genpipe import readset


def main():
    r = Report("readset")

    # ------------------------------------------------------------------ #
    r.section("the schema is the whole interface")

    columns = {c.name: c for c in readset.schema()}
    for required in ("Sample", "Readset", "RunType", "FASTQ1"):
        r.check(f"{required} is mandatory", columns[required].required)
    for optional in ("Library", "Run", "Lane", "BED", "BAM", "FASTQ2"):
        r.check(f"{optional} is not", not columns[optional].required)

    # ChIP-seq is the one family that adds mandatory columns, and they are
    # mandatory because nothing can derive them: which mark this is, and whether
    # it is called broad, narrow, or is the input control.
    chip = {c.name: c for c in readset.schema("chipseq")}
    r.check("chipseq adds MarkName", chip["MarkName"].required)
    r.check("and MarkType", chip["MarkType"].required)
    r.check("keeping the rest", "FASTQ1" in chip)

    long_read = {c.name for c in readset.schema("longread_dnaseq")}
    r.check("long reads have no RunType at all", "RunType" not in long_read)
    r.check("and one FASTQ column", "FASTQ" in long_read)

    spec = readset.schema_text()
    r.contains("the shareable spec names the columns", spec, "QualityOffset")
    r.contains("says which are required", spec, "required")
    r.check("and contains nothing real",
            "/lustre" not in spec and "@" not in spec)

    # ------------------------------------------------------------------ #
    r.section("readsets are found from filenames, never from contents")

    pairs = readset.pair_up([
        "S3382_S1_L001_R1_001.fastq.gz", "S3382_S1_L001_R2_001.fastq.gz",
        "S3385_1.fq.gz", "S3385_2.fq.gz",
        "S8613.R1.fastq", "S8613.R2.fastq",
        "lonely.fq.gz",
        "notes.txt",
    ])
    r.equal("four readsets from seven fastqs", len(pairs), 4)
    r.check("the non-fastq is ignored",
            not any("notes" in p[0] for p in pairs))
    mated = [p for p in pairs if p[2]]
    r.equal("three of them are pairs", len(mated), 3)
    r.check("_R1/_R2 pairs up",
            any(p[1].endswith("R1_001.fastq.gz") and p[2] for p in pairs))
    r.check("_1/_2 pairs up", any(p[1] == "S3385_1.fq.gz" and p[2] for p in pairs))
    r.check(".R1/.R2 pairs up", any(p[1] == "S8613.R1.fastq" and p[2] for p in pairs))
    r.check("and the unmated one stays unmated",
            any(p[1] == "lonely.fq.gz" and p[2] is None for p in pairs))

    # ------------------------------------------------------------------ #
    r.section("a directory becomes rows, with its guesses labelled")

    with tempfile.TemporaryDirectory() as tmp:
        for name in ("S1_L001_R1_001.fastq.gz", "S1_L001_R2_001.fastq.gz",
                     "S2_L001_R1_001.fastq.gz", "S2_L001_R2_001.fastq.gz"):
            open(os.path.join(tmp, name), "w").close()
        rows, warnings = readset.from_directory(tmp)
        r.equal("two readsets", len(rows), 2)
        r.check("both paired",
                all(row["RunType"] == "PAIRED_END" for row in rows))
        r.check("with absolute paths",
                all(os.path.isabs(row["FASTQ1"]) for row in rows))
        # The guess that most needs flagging: lanes of one sample must share a
        # Sample name, and a filename cannot say whether they do.
        r.check("and it says the Sample column is a guess",
                any("Sample column" in w for w in warnings))

        rendered = readset.render(rows)
        r.check("the header is the schema",
                rendered.splitlines()[0] == readset.header())
        r.equal("one line per readset plus the header",
                len(rendered.strip().splitlines()), 3)

        path = os.path.join(tmp, "readset.tsv")
        readset.write(path, rows)
        r.check("written", os.path.exists(path))
        # A readset file is hand-corrected after it is generated. Silently
        # replacing one destroys those edits, which is the whole reason the
        # Sample column was flagged above.
        try:
            readset.write(path, rows)
            r.check("overwriting is refused", False)
        except FileExistsError:
            r.check("overwriting is refused", True)

        r.equal("and it validates clean", readset.validate(path), [])

    with tempfile.TemporaryDirectory() as tmp:
        r.check("an empty directory says so, rather than writing nothing",
                readset.from_directory(tmp)[1][0].startswith("no FASTQ"))

    # ------------------------------------------------------------------ #
    r.section("validation is structural, and every check says which line")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.tsv")
        with open(path, "w") as f:
            f.write("\t".join(["Sample", "Readset", "RunType", "FASTQ1",
                               "FASTQ2"]) + "\n")
            f.write("A\trs1\tPAIRED_END\t/nowhere/a_R1.fq.gz\t\n")
            f.write("B\trs1\tBOTH_ENDS\t/nowhere/b.fq.gz\t\n")
            f.write("\trs2\tSINGLE_END\t/nowhere/c.fq.gz\t\n")
        problems = readset.validate(path, check_files=False)
        joined = " | ".join(problems)
        r.check("a PAIRED_END row with no FASTQ2 is caught",
                "PAIRED_END with no FASTQ2" in joined)
        r.check("a bogus RunType is caught", "BOTH_ENDS" in joined)
        r.check("a duplicate Readset id is caught", "more than once" in joined)
        r.check("an empty mandatory cell is caught", "Sample is empty" in joined)
        r.check("and every one names its line",
                all("line " in p for p in problems))

        # Separable, because the same file is worth checking before the data has
        # been staged, when every path is legitimately absent.
        with_files = readset.validate(path, check_files=True)
        r.check("missing files are only reported when asked for",
                len(with_files) > len(problems))

        missing_column = os.path.join(tmp, "short.tsv")
        with open(missing_column, "w") as f:
            f.write("Sample\tReadset\n A\trs1\n")
        r.check("a missing mandatory column stops everything else",
                any("missing required column" in p
                    for p in readset.validate(missing_column)))

    # ------------------------------------------------------------------ #
    r.section("synthetic rows satisfy the schema and contain nothing real")

    rows = readset.synthetic(samples=2, per_sample=2)
    r.equal("four readsets", len(rows), 4)
    r.equal("across two samples", len({row["Sample"] for row in rows}), 2)
    r.check("every sample name is obviously fake",
            all(row["Sample"].startswith("fake_") for row in rows))
    r.check("every path is obviously fake",
            all(row["FASTQ1"].startswith("/fake/") for row in rows))
    r.check("both run types appear, so code paths get exercised",
            {row["RunType"] for row in rows} == {"PAIRED_END", "SINGLE_END"})

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "synthetic.tsv")
        readset.write(path, rows)
        # The claim being tested: the validator cannot tell the difference
        # except on the one axis it is asked about.
        r.equal("structurally valid", readset.validate(path, check_files=False), [])
        r.check("and the files are honestly absent",
                bool(readset.validate(path, check_files=True)))

        facts = readset.summarise(path)
        r.equal("counted", facts["readsets"], 4)
        r.equal("grouped", facts["samples"], 2)
        r.equal("and multi-readset samples noticed",
                facts["multi_readset_samples"], 2)

    chip = readset.synthetic("chipseq", samples=1, per_sample=3)
    r.check("chipseq rows carry a mark",
            all(row["MarkName"] for row in chip))
    r.check("and a legal mark type",
            all(row["MarkType"] in readset.MARK_TYPES for row in chip))

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
