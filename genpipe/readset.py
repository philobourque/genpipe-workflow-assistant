"""Building and checking a readset file, from its structure alone.

A readset is one unit of sequencing: one sample, one library, one lane of one
run. A sample sequenced across three lanes has three readsets. GenPipes trims
and aligns each readset separately and then merges every readset belonging to
one sample into a single BAM, which is the whole reason the file exists -- it is
how you tell the pipeline that these eighteen FASTQ files are nine biological
samples, and which is which.

Why this module reads nothing
-----------------------------
Every operation here is defined over the SCHEMA -- which columns exist, what
type each is, which are mandatory -- and never over the contents of anybody's
data. That is not a limitation to work around; it is the point, and it is what
makes the assistant safe to point at a project directory.

The pairing logic works on FILENAMES (`_R1_`/`_R2_`), not on reads. The
validator checks structure (a PAIRED_END row must have a FASTQ2), not sequence.
And synthetic() produces rows that satisfy the schema with nothing real in them,
so the whole feature can be developed and tested before any real sequencing
exists and without a single genuine sample name in a test fixture:

    Sample  Readset  Library  RunType      FASTQ1           FASTQ2
    fake_A  rs001    lib01    PAIRED_END   /fake/r1.fq.gz   /fake/r2.fq.gz
    fake_A  rs002    lib01    SINGLE_END   /fake/r3.fq.gz
    fake_B  rs003    lib02    PAIRED_END   /fake/r4.fq.gz   /fake/r5.fq.gz

Anything written against those rows works unchanged on the real ones, because
the logic depends on where a column is and what is allowed in it -- not on what
is inside. It is a mail sorter: you need to know the postal code sits top-right;
you do not need to read the letters.

Standard library only.
"""
import os
import re

# ---------------------------------------------------------------------------
# The schema. One row per column, per family.
# ---------------------------------------------------------------------------

class Column:
    """One readset column: its name, its type, whether GenPipes requires it,
    and one line of plain English for whoever has to fill it in."""

    __slots__ = ("name", "type", "required", "note")

    def __init__(self, name, type_, required=False, note=""):
        self.name = name
        self.type = type_
        self.required = required
        self.note = note

    def __repr__(self):
        return f"<Column {self.name}:{self.type}{'!' if self.required else ''}>"


_ILLUMINA = [
    Column("Sample", "str", True,
           "the biological sample. Repeats across rows that belong together"),
    Column("Readset", "str", True,
           "unique per row. One sequencing unit: one sample, one lane"),
    Column("Library", "str", False, "library prep id; matters for duplicate marking"),
    Column("RunType", "enum", True, "PAIRED_END or SINGLE_END"),
    Column("Run", "str", False, "the sequencer run name"),
    Column("Lane", "int", False, "lane on the flow cell"),
    Column("Adapter1", "str", False, "forward adapter, passed to trimming"),
    Column("Adapter2", "str", False, "reverse adapter"),
    Column("QualityOffset", "int", False, "Phred offset; 33 for modern Illumina"),
    Column("BED", "path", False, "target regions, for capture data"),
    Column("FASTQ1", "path", True, "required unless a BAM is given instead"),
    Column("FASTQ2", "path", False, "required when RunType is PAIRED_END"),
    Column("BAM", "path", False, "converted to FASTQ when FASTQ1 is absent"),
]

# ChIP-seq adds two mandatory columns and nothing else. They are mandatory
# because the pipeline cannot guess them: which mark this readset is, and
# whether that mark is called as broad or narrow -- or is the input control.
_CHIPSEQ = _ILLUMINA[:2] + [
    Column("MarkName", "str", True, "the histone mark, or 'atac' for ATAC-seq"),
    Column("MarkType", "enum", True, "B (broad), N (narrow) or I (input)"),
] + _ILLUMINA[2:]

_LONGREAD = [
    Column("Sample", "str", True, "the biological sample"),
    Column("Readset", "str", True, "unique per row"),
    Column("Run", "str", False, "the sequencer run name"),
    Column("Flowcell", "str", False, "flowcell id"),
    Column("Library", "str", False, "library prep id"),
    Column("Summary", "path", False, "sequencing summary file"),
    Column("FASTQ", "path", True, "basecalled reads"),
    Column("FAST5", "path", False, "raw signal, for re-basecalling"),
    Column("BAM", "path", False, "aligned reads, if already aligned"),
]

_FAMILY = {
    "chipseq": _CHIPSEQ,
    "longread_dnaseq": _LONGREAD,
    "nanopore_covseq": _LONGREAD,
}

RUN_TYPES = ("PAIRED_END", "SINGLE_END")
MARK_TYPES = ("B", "N", "I")


def schema(pipeline=None):
    """The columns for a pipeline, in file order."""
    return list(_FAMILY.get(pipeline or "", _ILLUMINA))


def header(pipeline=None):
    """The header line, tab-separated, exactly as it must appear."""
    return "\t".join(c.name for c in schema(pipeline))


def schema_text(pipeline=None):
    """The schema as something you can send to a colleague.

    This is the artifact that makes the privacy claim concrete: it is enough to
    write, test and review every piece of code that touches a readset file, and
    it contains no sample name, no path and no data.
    """
    lines = [f"readset schema — {pipeline or 'illumina pipelines'}", ""]
    width = max(len(c.name) for c in schema(pipeline)) + 2
    for c in schema(pipeline):
        flag = "required" if c.required else "optional"
        lines.append(f"  {c.name:<{width}}{c.type:<7}{flag:<10}{c.note}")
    lines += ["", "Tab-separated. One row per readset. Rows sharing a Sample "
                  "are merged into one BAM."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Discovering readsets from filenames. Never from file contents.
# ---------------------------------------------------------------------------

_FASTQ = re.compile(r"\.(fastq|fq)(\.gz)?$", re.I)

# How Illumina and every downstream convention marks the two mates. Ordered
# most specific first: `_R1_` before `_1.` so a file called `sample_R1_001.fq.gz`
# is not matched on the `1` in `001`.
_MATE = [
    (re.compile(r"(.*)_R1(_.*|)$"), re.compile(r"(.*)_R2(_.*|)$")),
    (re.compile(r"(.*)_1$"), re.compile(r"(.*)_2$")),
    (re.compile(r"(.*)\.R1$"), re.compile(r"(.*)\.R2$")),
]


def _stem(name):
    """Strip the FASTQ suffixes, leaving the part that carries the naming."""
    return _FASTQ.sub("", name)


def pair_up(names):
    """Group FASTQ filenames into readsets. Returns [(base, r1, r2 or None)].

    Filenames only. This never opens a file, which is what makes it safe to run
    against a directory of somebody else's patient data, and also what makes it
    fast on Lustre where opening eighteen 4 GB files to confirm what their names
    already said would be minutes of IO for no information.
    """
    fastqs = sorted(n for n in names if _FASTQ.search(n))
    stems = {n: _stem(n) for n in fastqs}
    taken = set()
    out = []
    for name in fastqs:
        if name in taken:
            continue
        stem = stems[name]
        mate = None
        base = stem
        for first, second in _MATE:
            m = first.match(stem)
            if not m:
                continue
            base = m.group(1)
            want = second.pattern.replace("(.*)", re.escape(base), 1)
            for other in fastqs:
                if other in taken or other == name:
                    continue
                if re.fullmatch(want, stems[other]):
                    mate = other
                    break
            break
        taken.add(name)
        if mate:
            taken.add(mate)
        out.append((base, name, mate))
    return out


def _sample_of(base):
    """The sample name a readset base implies.

    The lane/run decorations come off (`_L001`, `_S3`, a trailing `_001`), and
    what is left is what the person doing the sequencing called the sample. A
    guess, and labelled as one everywhere it is used -- the readset file is
    reviewed before it is passed to genpipes, and this only has to be close
    enough that reviewing it is quicker than typing it.
    """
    stem = re.sub(r"_(L\d{2,3}|S\d+|\d{3})$", "", base)
    stem = re.sub(r"_(L\d{2,3}|S\d+)$", "", stem)
    return stem or base


def from_directory(directory, pipeline=None, library="", run="", lane="",
                   quality_offset="33", absolute=True):
    """Readset rows for the FASTQ files in a directory.

    Returns (rows, warnings). A row is a dict keyed by column name, so the
    caller writes it through write() and never has to know the column order --
    which is the one thing about this format that changes between pipelines.

    Warnings are structural observations, never assertions about the data: an
    unpaired file in a directory of pairs, a sample name that had to be guessed.
    """
    try:
        names = sorted(os.listdir(directory))
    except OSError as e:
        return [], [f"could not read {directory}: {e.strerror or e}"]

    pairs = pair_up(names)
    if not pairs:
        return [], [f"no FASTQ files in {directory}"]

    columns = {c.name for c in schema(pipeline)}
    rows, warnings = [], []
    singles = sum(1 for _, _, mate in pairs if mate is None)
    if singles and singles != len(pairs):
        warnings.append(f"{singles} of {len(pairs)} files have no mate — "
                        f"check whether those are really single-end")

    def path(name):
        full = os.path.join(directory, name)
        return os.path.abspath(full) if absolute else full

    for i, (base, first, mate) in enumerate(pairs, 1):
        row = {
            "Sample": _sample_of(base),
            "Readset": f"{_sample_of(base)}_rs{i:03d}",
            "Library": library,
            "RunType": "PAIRED_END" if mate else "SINGLE_END",
            "Run": run,
            "Lane": lane,
            "QualityOffset": quality_offset,
            "FASTQ1": path(first),
            "FASTQ2": path(mate) if mate else "",
        }
        if "FASTQ" in columns:          # the long-read family has no FASTQ1/2
            row["FASTQ"] = row.pop("FASTQ1")
            row.pop("FASTQ2", None)
            row.pop("RunType", None)
        if "MarkName" in columns:
            row["MarkName"] = ""
            row["MarkType"] = ""
            warnings.append("chipseq needs MarkName and MarkType on every row — "
                            "they cannot be derived from a filename")
            warnings = list(dict.fromkeys(warnings))
        rows.append(row)

    if len({r["Sample"] for r in rows}) == len(rows) and len(rows) > 1:
        warnings.append("every file became its own sample — if some of these "
                        "are lanes of one sample, edit the Sample column so "
                        "they match")
    return rows, warnings


def synthetic(pipeline=None, samples=2, per_sample=2):
    """Rows that satisfy the schema and contain nothing real.

    What development and testing run against. Any operation written against
    these works identically on real data, because the logic depends on the
    structure -- which column is where, what values are legal, which fields are
    mandatory -- and not on what is inside them.
    """
    columns = {c.name for c in schema(pipeline)}
    rows = []
    n = 0
    for s in range(samples):
        sample = f"fake_{chr(ord('A') + s)}"
        for r in range(per_sample):
            n += 1
            paired = (n % 3) != 2          # a single-end row in the mix
            row = {
                "Sample": sample,
                "Readset": f"rs{n:03d}",
                "Library": f"lib{s + 1:02d}",
                "RunType": "PAIRED_END" if paired else "SINGLE_END",
                "Run": "run000",
                "Lane": str(r + 1),
                "QualityOffset": "33",
                "FASTQ1": f"/fake/{sample}_rs{n:03d}_R1.fq.gz",
                "FASTQ2": f"/fake/{sample}_rs{n:03d}_R2.fq.gz" if paired else "",
            }
            if "FASTQ" in columns:
                row["FASTQ"] = row.pop("FASTQ1")
                row.pop("FASTQ2", None)
                row.pop("RunType", None)
            if "MarkName" in columns:
                row["MarkName"] = "atac" if pipeline == "atacseq" else "H3K27ac"
                row["MarkType"] = "I" if n % 3 == 0 else "N"
            rows.append(row)
    return rows


def render(rows, pipeline=None):
    """The file as text: header, then one line per row, tabs throughout."""
    columns = schema(pipeline)
    out = ["\t".join(c.name for c in columns)]
    for row in rows:
        out.append("\t".join(str(row.get(c.name, "") or "") for c in columns))
    return "\n".join(out) + "\n"


def write(path, rows, pipeline=None):
    """Write the readset file. Refuses to overwrite -- a readset file is
    hand-corrected after it is generated, and silently replacing one is
    destroying somebody's edits."""
    if os.path.exists(path):
        raise FileExistsError(path)
    with open(path, "w") as f:
        f.write(render(rows, pipeline))
    return path


# ---------------------------------------------------------------------------
# Validation. Structure only.
# ---------------------------------------------------------------------------

def validate(path, pipeline=None, check_files=True):
    """Structural problems with a readset file. Returns a list of strings.

    Every check here is answerable from the header and the cell values: a
    missing mandatory column, a RunType that is not one of the two legal words,
    a PAIRED_END row with no FASTQ2, a Readset id used twice, a FASTQ path that
    is not on disk. None of them require reading a read.

    `check_files` is separable because the same file is worth checking before
    the data has been copied to the cluster, when every path is legitimately
    absent.
    """
    problems = []
    try:
        with open(path) as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except OSError as e:
        return [f"could not read {path}: {e.strerror or e}"]
    if not lines:
        return [f"{os.path.basename(path)} is empty"]

    columns = schema(pipeline)
    names = lines[0].split("\t")
    index = {n: i for i, n in enumerate(names)}
    for column in columns:
        if column.required and column.name not in index:
            problems.append(f"missing required column {column.name}")
    if problems:
        return problems

    seen = set()
    directory = os.path.dirname(os.path.abspath(path))
    for n, line in enumerate(lines[1:], 2):
        cells = line.split("\t")

        def cell(name):
            i = index.get(name)
            return cells[i].strip() if i is not None and i < len(cells) else ""

        for column in columns:
            if column.required and not cell(column.name):
                problems.append(f"line {n}: {column.name} is empty")

        readset = cell("Readset")
        if readset and readset in seen:
            problems.append(f"line {n}: Readset {readset!r} is used more than once")
        seen.add(readset)

        run_type = cell("RunType")
        if "RunType" in index:
            if run_type and run_type not in RUN_TYPES:
                problems.append(f"line {n}: RunType {run_type!r} is not "
                                f"{' or '.join(RUN_TYPES)}")
            if run_type == "PAIRED_END" and not cell("FASTQ2") and not cell("BAM"):
                problems.append(f"line {n}: PAIRED_END with no FASTQ2")

        mark = cell("MarkType")
        if "MarkType" in index and mark and mark not in MARK_TYPES:
            problems.append(f"line {n}: MarkType {mark!r} is not "
                            f"{'/'.join(MARK_TYPES)}")

        if check_files:
            for column in ("FASTQ1", "FASTQ2", "FASTQ", "BAM", "BED"):
                value = cell(column)
                if not value:
                    continue
                full = value if os.path.isabs(value) else os.path.join(directory, value)
                if not os.path.exists(full):
                    problems.append(f"line {n}: {column} is not on disk — {value}")
    return problems


def summarise(path, pipeline=None):
    """What a readset file says, without saying what is in it.

    Sample and readset COUNTS, the run types present, whether any sample has
    more than one readset. Enough to confirm the file describes the experiment
    you meant, and nothing that would be awkward to put on a screen in a shared
    office.
    """
    try:
        with open(path) as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except OSError:
        return None
    if len(lines) < 2:
        return None
    names = lines[0].split("\t")
    index = {n: i for i, n in enumerate(names)}
    samples, types = {}, {}
    for line in lines[1:]:
        cells = line.split("\t")

        def cell(name):
            i = index.get(name)
            return cells[i].strip() if i is not None and i < len(cells) else ""

        samples[cell("Sample")] = samples.get(cell("Sample"), 0) + 1
        rt = cell("RunType") or "—"
        types[rt] = types.get(rt, 0) + 1
    multi = sum(1 for n in samples.values() if n > 1)
    return {
        "readsets": len(lines) - 1,
        "samples": len(samples),
        "run_types": types,
        "multi_readset_samples": multi,
        "columns": names,
    }
