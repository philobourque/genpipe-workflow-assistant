"""Which flags a pipeline takes, and which of them it cannot run without.

Read out of `genpipes <pipeline> --help`, never decided here. That is the whole
point of the module, and it is the opposite of slots.py's: slots.py holds the
mapping no install will tell you (which protocol needs a pairs file, which inis
a protocol stacks), while everything in THIS file is printed by GenPipes about
itself and would be a lie the moment the module version changed.

argparse prints the answer in the usage line and nowhere else. Required options
appear bare; everything optional is wrapped in brackets:

    usage: genpipes ampliconseq [-h] [--clean] -c CONFIG [CONFIG ...]
                                [-o OUTPUT_DIR] [-s STEPS]
                                -r READSETS_FILE [-d DESIGN_FILE]

So `-c` and `-r` are required and `-d` is not -- which is worth stating plainly,
because "required" here means REQUIRED BY ARGPARSE. It is a narrower claim than
"the run needs it". ampliconseq's `-d` is optional to argparse and still
mandatory for any run that reaches the `asva` step, and no --help anywhere says
so. The two facts live in two modules on purpose and neither can answer for the
other: this one is version-exact and shallow, slots.py is version-blind and
knows what the steps do.

WHAT THIS IS USED FOR, in order of how much it matters:

  1. A command with no `-c` is refused before the gate draws a box for it.
     slots.gaps() never checked the config stack -- it has no entry for it --
     so a generation missing the one flag every pipeline demands looked
     complete, went to the person as READY TO SUBMIT, and failed at generation
     after they approved it.

  2. The gate says which flags are required, on the rows themselves.

  3. A flag the pipeline does not take is dropped from the rows /modify offers
     (modify.rows_for). Only ever SUBTRACTIVE, and only ever from rows nothing
     has filled in: --help lists `-d` on covseq, where no step reads a design,
     so a panel built FROM --help would offer a design on every pipeline in the
     install. The tables decide which absences are real questions; this removes
     the ones that could not be questions, because the flag does not exist.

This module deals in FLAGS and knows nothing about /modify's rows. The
translation lives on the other side, in mirror._ROW_OF, which already holds it
in both spellings -- and it has to be that way round, since modify imports gate
and a row table here would close the circle.

Standard library only, like slots.py, gate.py, modify.py and mirror.py, so it is
checkable in CI without a GenPipes install -- which is also why it takes help
TEXT rather than running anything. modify.steps_from_help already works this
way, for the same reason: fetching is the caller's problem (runs.pipeline_help),
parsing is this module's.
"""
import re

# A token that is a flag rather than a value or a metavar. Anchored at both
# ends: `CONFIG` is a metavar, `1-5` is a step range, `{pbs,batch}` is a choice
# list, and none of them may be mistaken for an option this pipeline accepts.
_FLAG = re.compile(r"^--?[A-Za-z][A-Za-z0-9_-]*$")

# The usage block: from `usage:` to the first blank line. argparse wraps it over
# as many lines as it needs and always terminates it with a blank one.
_USAGE = re.compile(r"^usage:(.*?)(?:\n[ \t]*\n|\Z)", re.S | re.M)


class Usage:
    """One pipeline's flag surface, as `--help` printed it.

    flags      every option the pipeline accepts, canonical spelling, in the
               order the usage line lists them
    required   the subset argparse will not run without

    Both are canonical spellings -- see _canonical for which of `-c` and
    `--config` wins and why. Ask through takes()/requires() rather than testing
    membership directly, so a model that wrote `--config` is not reported as
    having omitted `-c`.
    """

    __slots__ = ("pipeline", "flags", "required", "_canon")

    def __init__(self, pipeline="", flags=(), required=(), canon=None):
        self.pipeline = pipeline or ""
        self.flags = tuple(flags)
        self.required = tuple(required)
        self._canon = dict(canon or {})

    def __bool__(self):
        """False when nothing was parsed.

        Callers treat an unreadable --help as NO OPINION -- never as "this
        pipeline takes no flags", which would hide every row on the gate, and
        never as "nothing is required", which would let an incomplete command
        through. Both failures are silent, so the falsy Usage exists to make the
        check impossible to forget: `if usage:` is the only way in.
        """
        return bool(self.flags)

    def __repr__(self):
        return (f"<Usage {self.pipeline} {len(self.flags)} flags, "
                f"requires {' '.join(self.required) or 'nothing'}>")

    def canonical(self, flag):
        """The spelling this module reports for `flag`, or the flag itself."""
        return self._canon.get(str(flag or ""), str(flag or ""))

    def takes(self, flag):
        return self.canonical(flag) in self.flags

    def requires(self, flag):
        return self.canonical(flag) in self.required

    def lacking(self, command):
        """Required flags the command does not write, canonical, in usage order.

        Empty when this Usage is empty, which is the no-opinion rule above: an
        unreadable --help must not manufacture a refusal.
        """
        if not self:
            return ()
        written = {self.canonical(flag) for flag in flags_in(command)}
        return tuple(flag for flag in self.required if flag not in written)


def flags_in(command):
    """Every flag a command line writes, in order, as written.

    Whitespace-split rather than shlex: this answers "was `-c` given", and the
    one thing that could change that answer is a quoted value that both starts
    with a dash and looks like an option -- which would have to be a deliberate
    `-c "-r foo"` to matter. shlex would not help anyway, since it strips the
    quotes that made the value a value.

    `--flag=value` counts as `--flag`, matching how mirror._groups reads it.
    """
    out = []
    for token in str(command or "").split():
        token = token.split("=", 1)[0]
        if _FLAG.match(token):
            out.append(token)
    return tuple(out)


def _canonical(group):
    """Which spelling of an option this module names it by: the short one.

    `-c, --config` is reported as `-c`, and `--genpipes_file, -g` as `-g`. The
    short form wins because it is the spelling everything else in this repo uses
    -- modify.FLAG_OF, mirror._LABEL, the gate's own parsing, and the commands
    people actually write -- so canonicalising the other way would mean
    translating at every boundary.

    A long-only option (`--clean`, `--sanity-check`) is its own canonical form.
    """
    for flag in group:
        if not flag.startswith("--"):
            return flag
    return group[0]


def _alias_groups(text):
    """[[spelling, ...]] from the options section: which flags are one flag.

    argparse lists an option's spellings together on the line that documents it,
    at an indent of exactly two spaces, with the wrapped help text far to the
    right:

          -c, --config CONFIG [CONFIG ...]
                                config INI-style list of files; ...
          --genpipes_file, -g GENPIPES_FILE

    Reading these is not cosmetic. The usage line prints ONE spelling per
    option, and for the flag this project cares most about it prints the long
    one: `[--genpipes_file GENPIPES_FILE]`, never `-g`. Without the aliases,
    `-g cmd.sh` -- what every command in this repo writes -- would come back as
    a flag ampliconseq does not accept.
    """
    for line in str(text or "").splitlines():
        if not line.startswith("  ") or line[2:3] != "-" or line[3:4] == " ":
            continue
        group = []
        for token in line[2:].split():
            token = token.rstrip(",")
            if not _FLAG.match(token):
                break                 # the metavar; the spellings are over
            group.append(token)
        if group:
            yield group


def _usage_flags(block):
    """(every flag, the unbracketed ones) from a usage block.

    Depth is counted over square brackets only. Braces and angle brackets do
    appear -- `[--container {wrapper, singularity} <IMAGE PATH>]` -- and are
    left alone deliberately: nothing inside either can start with a dash, so
    they cannot produce a false flag, and tracking them would only add a way to
    get the depth wrong.

    A word's depth is where it STARTED, which is what makes `-c CONFIG [CONFIG
    ...]` come out right: `-c` is required and the bracketed repeat that follows
    it is the same option's nargs, not a second optional one.
    """
    every, required = [], []
    depth, word, opened = 0, [], 0

    def flush():
        nonlocal word
        token = "".join(word)
        word = []
        if not _FLAG.match(token):
            return
        every.append(token)
        if opened == 0:
            required.append(token)

    for char in block:
        if char == "[":
            flush()
            depth += 1
        elif char == "]":
            flush()
            depth = max(0, depth - 1)
        elif char.isspace():
            flush()
        else:
            if not word:
                opened = depth
            word.append(char)
    flush()
    return every, required


def _names(block, pipeline):
    """Whether this usage block is the one for `pipeline`.

    A guard against the most likely wrong answer available, and it is a
    genuinely dangerous one. Asking about a pipeline that does not exist does
    not fail -- argparse prints the TOP-LEVEL usage and exits:

        usage: genpipes [-h] [-v] [-s {bash,zsh,tcsh}]
                        {ampliconseq,chipseq,...,tools} ...
        genpipes: error: argument command: invalid choice: 'nope'

    which parses perfectly and describes a completely different parser. Taken
    for a pipeline's flag surface it says this pipeline accepts three flags and
    requires none, so /modify drops every row it could have offered, and `-s`
    -- shell completion here, the STEP RANGE one level down -- silently changes
    meaning. gate.build_proposal reads its pipeline name off the command with a
    regex, so a command this project half-understands is exactly what lands
    here.

    The check is the same one a reader makes: the usage line has to say the
    pipeline's name right after `genpipes`.
    """
    words = str(block or "").split()
    return len(words) > 1 and words[0] == "genpipes" and words[1] == pipeline


def read(text, pipeline=""):
    """Parse `genpipes <pipeline> --help` into a Usage.

    Returns an empty (falsy) Usage for anything that is not argparse help --
    an empty string, an Lmod error, a stack trace. Never raises and never
    guesses: every caller's fallback is to carry on with what slots.py knows,
    which is what happens on a laptop with no GenPipes on it.
    """
    match = _USAGE.search(str(text or ""))
    if not match:
        return Usage(pipeline)
    if pipeline and not _names(match.group(1), pipeline):
        return Usage(pipeline)

    every, required = _usage_flags(match.group(1))
    canon = {}
    for group in _alias_groups(text):
        head = _canonical(group)
        for spelling in group:
            canon[spelling] = head
    # Options the usage line shows but the options section never documented get
    # to be their own canonical form, so an undocumented flag is still a flag
    # rather than being dropped.
    for flag in every:
        canon.setdefault(flag, flag)

    def unique(flags):
        return tuple(dict.fromkeys(canon[flag] for flag in flags))

    return Usage(pipeline, unique(every), unique(required), canon)
