"""The directories a conversation has pointed at.

WHAT THIS MODULE USED TO BE, and why almost none of it is left.

It began as the place that recognised an ordinary sentence as the beginning of
a run: an intent classifier over opening words (run / question / ambiguous), a
19-row regex table mapping a scientific description to a pipeline and protocol,
a next-question chooser, and a `Preparation` record carrying the resolved state
of a run being assembled. Every one of those is gone, in two stages.

The first stage removed the intent classifier and the next-question chooser.
"run" appears in "how do I run dnaseq?", and "a quick AmpliconSeq test on the
CIT data" is an intention no keyword table recognises; meanwhile dictating the
next question put a questionnaire back one indirection away. Reading intent and
choosing what to ask are the agent's job, and it is better at both.

The second stage removed the rest, and it is worth recording what forced it.
The table was mapping descriptions to a PROTOCOL -- "compare gene expression"
became `-t stringtie` -- and cli._settled was handing that to the model under
the header "Do not ask about any of it again". The system prompt forbids the
model from guessing a protocol, in as many words, because a guessed one
produces a run that completes successfully and answers a different question;
deterministic code was guessing it on the model's behalf and suppressing the
question. Literal name matching was no better once measured:

    "should I use rnaseq or chipseq?"  ->  Pipeline: chipseq. Protocol: chipseq.
    "I do NOT want chipseq"            ->  Pipeline: chipseq. Protocol: chipseq.

A comparison settled both fields and a refusal settled the thing refused,
because matching a word proves it was TYPED, not that it was CHOSEN. Telling
those apart is a reading task. It belongs to the agent, and a cleverer pattern
here would only relocate the error.

It was also unnecessary. The justification for remembering anything was that
`intake.brief()` parses only the line in front of it -- true of brief(), and
not true of the model, which agent.run() replays the entire thread to.

WHAT IS LEFT is the one fact deterministic code consumes itself: which
directories to search for candidate files, so intake never falls back to
describing the process's own working directory (AGENT-FIXES.md defect 1).

Standard library plus intake, which is also stdlib-only. track() and context()
live HERE rather than in cli.py, where they used to, for the reason the whole
package is arranged this way: cli.py imports biomni, so anything left in it
cannot be tested in CI. These are pure functions over a line and a Preparation
and never needed an agent -- and the one property they now exist to guarantee,
that a mention is never recorded as a selection, is exactly the sort that
should be checked on every push.
"""
from . import intake

# Rows nobody should ever be asked about. A run with no -s runs every step, and
# the cluster ini for the machine you are logged into is not a decision -- it is
# the answer. Asking about either is how a conversation turns into a form.
ASSUMED = ("steps", "config", "output")


class Preparation:
    """The directories a conversation has pointed at. Nothing else.

    Carried across turns by the caller. It holds no pipeline, no protocol and
    no filenames: see the module docstring for what those became and why
    keeping them was actively harmful rather than merely redundant.

    Directories ACCUMULATE. A single `project_dir` was last-one-wins, so a path
    mentioned in passing late in a conversation -- an output directory, most
    often -- silently redirected every later lookup away from the data. Several
    roots cost an extra option in a panel; the wrong single root costs a run
    built from somebody else's files.

    Whether a mentioned path is admitted at all is decided by the caller, from
    the filesystem (intake.holds_candidates), never from the wording.
    """

    __slots__ = ("directories",)

    def __init__(self, directories=()):
        self.directories = list(directories)

    def remember_dir(self, directory):
        """Add a directory to search, keeping the order they were mentioned in
        and never adding one twice."""
        if directory and directory not in self.directories:
            self.directories.append(directory)
        return self

    @property
    def directory(self):
        """The first directory mentioned, for callers that can only take one.

        First rather than last, and that is the whole point of the change: the
        one somebody led with is not displaced by a path mentioned in passing
        afterwards.
        """
        return self.directories[0] if self.directories else None

    def as_dict(self):
        return {"directories": list(self.directories)}

    def __repr__(self):
        return f"<Preparation dirs={self.directories}>"


def context(directories):
    """Directories mentioned so far, as context for the model -- or None.

    WHAT THIS NO LONGER DOES, and why it is a deletion rather than a better
    detector. It used to assert a pipeline, a protocol and three filenames, all
    matched out of prose, under the header "Do not ask about any of it again".
    Two things were wrong with that.

    The first is that a whole-word match proves the words were TYPED, not that
    they were CHOSEN. Measured, on real sentences:

        "should I use rnaseq or chipseq?"  ->  Pipeline: chipseq. -t chipseq.
        "I do NOT want chipseq"            ->  Pipeline: chipseq. -t chipseq.
        "does dnaseq need a pairs file?"   ->  Pipeline: dnaseq.

    A comparison settled both fields; a refusal settled the thing refused. No
    regex fixes that, because telling a mention from a selection is a reading
    task -- which is the agent's, and putting a smarter classifier here would
    only move the same mistake one indirection away.

    The second is that it was never needed. The docstring used to justify
    itself with "intake.brief() parses only the line in front of it, so a
    protocol named three turns ago is invisible" -- true of brief(), and not
    true of the MODEL: agent.run() replays the whole thread
    (`prior + [HumanMessage(...)]`), so everything said earlier is already in
    front of it. This was re-asserting what the model could read, in an
    imperative voice that overrode its reading.

    WHAT SURVIVES is provenance: the directories somebody has actually named,
    so intake has somewhere to look other than the process's own cwd
    (AGENT-FIXES.md defect 1), and so a path mentioned four turns ago is still
    on the table.

    It asserts nothing about them. Not that the data is there, not that one of
    them is "the" project directory, and not that nowhere else may be looked
    at -- the previous version said all three, which is how naming an output
    path once redirected every later lookup. Which of these matters, and
    whether any of them does, is a reading the agent makes from the sentence
    that named it.
    """
    if not directories:
        return None
    listed = ([directories] if isinstance(directories, str)
              else list(directories))
    if not listed:
        return None
    return ("Directories mentioned so far in this conversation, in the order "
            "they came up:\n"
            + "\n".join(f"- {d}" for d in listed)
            + "\nThis is only a record of what was named. Some may be data, "
              "one may be an output directory, one may have been ruled out. "
              "Work out which is which from what was said, and look before "
              "asking.")


def track(state, line):
    """Note the directories a line points at, and return (state, context).

    All that is left of what used to be cli._preparing(), and now of the fact
    list cli._settled() carried too. What went, and why, is written up in
    context() above: a whole-word match proves a word was typed, not chosen,
    and the model already has the whole thread to read it in. Nothing here
    decides what the run is any more.

    THIS IS PROVENANCE, NOT A SEARCH ROOT. Every real directory somebody names
    is remembered, and nothing here decides which of them the data is in.

    There was briefly a filesystem test in this spot -- keep the path only if
    it directly contains a readset, a design or a pairs file -- and it was the
    same mistake as the regex it replaced, moved one layer down. Deciding from
    a listdir what the user meant by naming a path is still classification, and
    it was wrong about ordinary layouts: readsets under raw_reads/, a run
    assembled from three directories, a readset not written yet. Each would
    have been silently refused.

    So the cases stay open, and the agent closes them, because it can see the
    sentence and this cannot:

        "put the output in /scratch/out"   remembered as mentioned. The agent
                                           reads "output" and does not go
                                           looking for a readset there
        "not the old /scratch/jan run"     remembered as mentioned. The agent
                                           reads "not"
        "is it ~/proj/a or ~/proj/b?"      both remembered, neither chosen --
                                           WHICH is the person's to answer

    They ACCUMULATE rather than overwrite. Last-one-wins meant the most
    recently uttered path silently became "the" project directory, so naming an
    output path once redirected every later lookup away from the data.
    """
    for named in intake.find_directories(line):
        state.remember_dir(named)
    return state, context(state.directories)
