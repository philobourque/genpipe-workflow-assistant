"""The closed set of actions the model may ask the application to perform.

WHAT THIS IS NOT, first, because the distinction is the whole design.

This is NOT an intent taxonomy. Nothing here inspects what a person typed, and
nothing here maps a sentence to an action. There is no entry that says "why"
plus "failed" means diagnose. The table is a list of things the model is
*allowed* to call once it has decided, on its own, that calling one is useful --
the same relationship a function signature has to the decision to call it.

The flow it belongs to:

    user prose
        v
    the main model reads the whole conversation and decides
        v
    it answers, or asks, or EMITS A STRUCTURED CALL
        v
    only then does anything here run

So the input to this module is always a call the model wrote, parsed out of its
own <execute> block by gate.capability_request(). It is never the user's text.
That is not a convention -- it is the only thing the parser is ever handed, and
tests/test_capability.py asserts that prose containing every trigger word
anybody could imagine produces no capability at all.

WHY A TABLE AT ALL, then. Because the model choosing an action is not the same
as the action being safe or even possible. A closed set means:

  * a name the model invented is refused rather than run
  * arguments are checked before a handler sees them
  * every entry declares what it touches, so the read-only ones can be enabled
    on their own and the rest cannot arrive by accident

WHY IT IS STDLIB-ONLY. Same reason gate.py is: the question "which actions
exist, and what may they touch" is a safety question, and it should be
answerable in CI in milliseconds without installing biomni. The BINDING of a
name to a method lives in agent.py, where the methods are; agent._CAPABILITY
and this table are asserted to cover exactly the same names, so neither can
grow an entry the other has not heard of.
"""

# What an action is allowed to touch. Declared per entry so that enabling a
# class is one decision rather than an audit of every handler.
#
# READS      queries state and explains it. No side effect a person would
#            notice, beyond a cached status the next /list would have written
#            anyway.
# LOCAL      writes something of ours -- the registry, a file in the project.
#            Reversible, but it is a change and it is confirmed.
# SCHEDULER  changes what Slurm will do. Always confirmed, never enabled
#            without that confirmation being the same one the typed command
#            uses.
READS = "reads"
LOCAL = "local"
SCHEDULER = "scheduler"

# Phase 7 enables READS and nothing else. The other two classes are declared so
# that the shape is settled before anything needs them -- not so they can be
# switched on quietly.
ENABLED = (READS,)


# The argument that is not an argument. Any call may carry `more=True`, which
# is the model saying "hand the result back to me, I am not finished". It is
# not passed to a handler and no entry declares it -- it describes the TURN,
# not the action. See CONTINUES below and agent's capability node.
CONTINUE = "more"


class Capability:
    """One callable action: its name, its arguments, and what it may touch.

    `summary` is written for the model, not for a person. It is the whole of
    what the model is told about the action, so it says what the action DOES
    and never when to use it -- "show every job in a run and its state", not
    "call this when the user asks about jobs". The second phrasing would be an
    intent rule wearing a docstring, and the model is better at deciding when
    than any sentence here could be.

    `renders` IS THE ONE THAT DECIDES WHERE THE TURN ENDS, and it is a fact
    about the action rather than a preference about behaviour: this action
    prints the complete user-facing answer itself -- the same panel the
    matching slash command draws, with its own Actions block -- so when it
    returns, the person has been answered.

    Every entry in the table below is one of those today. The field exists
    because the next one might not be: a lookup that returns a fact with no
    screen of its own is genuine intermediate evidence and must go back to the
    model, which is the only way the answer ever gets written. Marking those
    two kinds the same is what let a rendered diagnosis be treated as a tool
    observation and a rendered list of twenty runs be followed by "no runs
    currently recorded".

    `ends_turn` IS A SEPARATE CLAIM FROM `renders`, and separating them is what
    this field exists for. `renders` says the action draws a panel somebody
    reads. `ends_turn` says the result is normally the whole of what was asked
    for, so the reasoning loop stops there. Six of the seven entries are both.
    `where` is the one that is neither the same nor reducible: it draws a real
    panel AND is orientation a model reaches for on its way to something else,
    which is exactly the case the paragraph above anticipated.

    Both are declared explicitly on every entry -- there is no default here on
    purpose. A new capability that inherited "terminal" silently would fail the
    way `where` did: the model calls it mid-task, the turn ends, and nothing on
    screen says the work was abandoned. Classifying it is one word and it is
    the author's to write down.
    """

    __slots__ = ("name", "args", "required", "kind", "summary", "renders",
                 "ends_turn")

    def __init__(self, name, args=(), required=(), kind=READS, summary="",
                 renders=False, ends_turn=False):
        self.name = name
        self.args = tuple(args)
        self.required = tuple(required)
        self.kind = kind
        self.summary = summary
        self.renders = bool(renders)
        self.ends_turn = bool(ends_turn)

    @property
    def enabled(self):
        return self.kind in ENABLED

    def __repr__(self):
        return f"<Capability {self.name}({', '.join(self.args)}) {self.kind}>"


def _c(name, args=(), required=(), kind=READS, summary="", renders=False,
       ends_turn=None):
    """One table row. `ends_turn` is required -- see Capability.

    Not defaulted. A missing classification raises here rather than picking a
    value, because both answers are wrong for some capability and the one that
    is wrong silently is the one that ends a turn nobody asked to end.
    """
    if ends_turn is None:
        raise ValueError(
            f"{name}: ends_turn must be stated. True if this action is normally "
            f"the whole answer, False if it is evidence on the way to one.")
    return Capability(name, args, required, kind, summary, renders, ends_turn)


# The table. Read-only for now; every entry is backed by a method the
# equivalent slash command already calls, which is the rule that keeps the two
# routes from becoming two implementations.
TABLE = {
    c.name: c for c in (
        _c("check_run", ("name",), ("name",), READS,
           "the overall state of one run as the scheduler reports it: how many "
           "jobs are queued, running, done or broken, and the root cause if "
           "something failed. `name` may be \"all\" for every run.",
           renders=True, ends_turn=True),
        _c("inspect_jobs", ("name", "failed"), ("name",), READS,
           "every individual job inside a run and its state, grouped by step. "
           "`failed` true narrows it to the ones that did not complete.",
           renders=True, ends_turn=True),
        _c("diagnose_run", ("name", "question"), ("name",), READS,
           "read the logs of a run's failed jobs and explain what went wrong. "
           "Gathers the evidence first, then reasons over it. `question` "
           "narrows the investigation.",
           renders=True, ends_turn=True),
        _c("show_run", ("name",), ("name",), READS,
           "the exact GenPipes command a run is, flag by flag, and what can "
           "still be done to it.",
           renders=True, ends_turn=True),
        _c("list_runs", (), (), READS,
           "the runs that currently need attention or are still going.",
           renders=True, ends_turn=True),
        _c("run_history", (), (), READS,
           "every run ever recorded, including finished and abandoned ones.",
           renders=True, ends_turn=True),
        # THE ONE INTERMEDIATE ENTRY. It draws a panel, so renders is True;
        # it is almost never the whole of what was asked, so ends_turn is not.
        #
        # A model preparing a run wants to know where the artifacts will land
        # before it writes a command, which is a reasonable thing to want and
        # was the only way to get it. With ends_turn tied to renders, asking
        # ended the turn: the panel printed, the plan was abandoned, and
        # nothing said so. See the note above _run_capability.
        #
        # Someone who asks "where does this write?" therefore costs one extra
        # model call -- the rows come back as an observation and the model says
        # a sentence over them. That is the right trade against silently
        # dropping a run somebody asked for.
        _c("where", (), (), READS,
           "the directories this session is using: the cluster, the working "
           "directory, the run registry and the checkpoint store.",
           renders=True, ends_turn=False),
    )
}

# Names only, for the parser. gate.py needs to know which calls are capability
# calls without knowing anything about what they do.
NAMES = frozenset(TABLE)


def get(name):
    """The Capability for `name`, or None. Never raises on a bad name."""
    return TABLE.get(str(name or ""))


def continues(args):
    """(action arguments, whether the model said it is not finished).

    `more=True` is stripped here rather than declared on every entry, because
    it says nothing about the action -- it is the model stating, in the same
    structured call it was already writing, that the panel this produces is not
    the end of what it was asked for. "Check this run and if it failed diagnose
    it" is check_run(name=..., more=True); "how are my runs doing" is
    list_runs().

    THE DEFAULT IS THE SAFE ONE. Absent means finished, so a model that forgets
    the flag ends the turn with the complete canonical answer already on
    screen. The opposite default is what the old unconditional edge back to
    `generate` amounted to, and it cost 36 model calls and 185 seconds after
    the answer was already rendered.

    Anything other than a literal true is read as absent: this decides whether
    a person gets their prompt back, and a string "maybe" must not be a reason
    to keep going.
    """
    args = dict(args or {})
    more = args.pop(CONTINUE, None)
    return args, more is True


def validate(name, args):
    """(Capability, None) if this call is well formed, else (None, complaint).

    Runs BEFORE any handler sees the arguments, and the complaint is written to
    be read by the model -- it goes back as an observation, so it has to say
    what was wrong in terms the next attempt can act on.

    Four ways a call can be wrong, and they are genuinely different:

      unknown name      the model invented an action. Refused, and the real
                        ones are named, because a model that guessed once will
                        guess again without a list.
      not enabled       a real action whose class is switched off. Said as
                        such rather than as "no such thing", which would be a
                        lie the model would keep tripping over.
      unknown argument  a typo, or an argument from a different action.
      missing argument  the commonest, and the one worth naming precisely.
    """
    spec = get(name)
    if spec is None:
        offered = ", ".join(sorted(n for n, c in TABLE.items() if c.enabled))
        return None, (f"There is no action called {name!r}. "
                      f"The ones that exist are: {offered}.")
    if not spec.enabled:
        return None, (f"{spec.name} exists but is not available here. "
                      f"Anything that changes a run or the scheduler is done "
                      f"by the person, through a typed command.")
    args, _ = continues(args)
    unknown = [k for k in args if k not in spec.args]
    if unknown:
        return None, (f"{spec.name} takes {', '.join(spec.args) or 'no arguments'}"
                      f" — it has no {', '.join(sorted(unknown))}.")
    missing = [k for k in spec.required if not str(args.get(k) or "").strip()]
    if missing:
        return None, f"{spec.name} needs {', '.join(missing)}."
    return spec, None


def catalogue():
    """The enabled actions, as lines for the system prompt.

    Generated from the table rather than written into the prompt by hand, so
    the actions the model is told about and the actions that exist cannot
    diverge -- the same argument as slots.requirements_note(), for the same
    reason.
    """
    out = []
    for name in sorted(TABLE):
        spec = TABLE[name]
        if not spec.enabled:
            continue
        signature = ", ".join(
            f"{a}=..." if a in spec.required else f"[{a}=...]" for a in spec.args)
        out.append(f"  {spec.name}({signature})\n      {spec.summary}")
    return "\n".join(out)


def protocol():
    """How a call ends a turn, stated for the model. Generated, not written.

    Derived from the table's own `ends_turn` flags for the same reason
    catalogue() is generated: what the model is told and what the code does
    cannot be allowed to drift, and this particular sentence decides whether a
    person gets their prompt back.

    READ OFF ends_turn, NOT renders. Those were the same field once, and the
    sentence this produced then told the model that every drawn panel was the
    end of its turn -- true of six entries and false of `where`, which is
    orientation rather than an answer.

    It also claimed each of them prints "the same panel the matching command
    prints". That is the rule for the other six (agent._CAPABILITY handlers
    call the same methods the slash commands call) and it is not true of
    `where`: cli._cmd_where builds its own richer rows and never goes through
    this table at all. The claim is dropped rather than qualified -- what the
    model needs to know is which calls END, and the panel's provenance is not
    its business.
    """
    drawn = sorted(n for n, c in TABLE.items() if c.enabled and c.ends_turn)
    more_first = sorted(n for n, c in TABLE.items()
                        if c.enabled and not c.ends_turn)
    if not drawn:
        return ""
    out = (
        f"EACH OF THESE ANSWERS THE PERSON ITSELF. {', '.join(drawn)} draw the "
        f"complete answer on screen, Actions block included, and the turn ENDS "
        f"there -- you are not called again to summarise it, and you must not, "
        f"because they are already reading it.\n\n"
        f"If the panel is not the whole of what you were asked for, say so IN "
        f"THE CALL by adding {CONTINUE}=True:\n\n"
        f"    check_run(name=\"run-a\", {CONTINUE}=True)\n\n"
        f"That hands the result back to you so you can carry on -- which is "
        f"what \"check it, and diagnose it if it failed\" needs, and what "
        f"\"how are my runs doing\" does not. Leave it off when the panel "
        f"answers the question.")
    if more_first:
        out += (
            f"\n\n{', '.join(more_first)} {'is' if len(more_first) == 1 else 'are'} "
            f"different: {'it draws' if len(more_first) == 1 else 'they draw'} a "
            f"panel too, but the result comes back to you and the turn does NOT "
            f"end. Use {'it' if len(more_first) == 1 else 'them'} freely on the "
            f"way to something else -- you do not need {CONTINUE} to carry on "
            f"afterwards, and you should carry on.")
    return out
