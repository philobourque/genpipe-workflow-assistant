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


class Capability:
    """One callable action: its name, its arguments, and what it may touch.

    `summary` is written for the model, not for a person. It is the whole of
    what the model is told about the action, so it says what the action DOES
    and never when to use it -- "show every job in a run and its state", not
    "call this when the user asks about jobs". The second phrasing would be an
    intent rule wearing a docstring, and the model is better at deciding when
    than any sentence here could be.
    """

    __slots__ = ("name", "args", "required", "kind", "summary")

    def __init__(self, name, args=(), required=(), kind=READS, summary=""):
        self.name = name
        self.args = tuple(args)
        self.required = tuple(required)
        self.kind = kind
        self.summary = summary

    @property
    def enabled(self):
        return self.kind in ENABLED

    def __repr__(self):
        return f"<Capability {self.name}({', '.join(self.args)}) {self.kind}>"


def _c(name, args=(), required=(), kind=READS, summary=""):
    return Capability(name, args, required, kind, summary)


# The table. Read-only for now; every entry is backed by a method the
# equivalent slash command already calls, which is the rule that keeps the two
# routes from becoming two implementations.
TABLE = {
    c.name: c for c in (
        _c("check_run", ("name",), ("name",), READS,
           "the overall state of one run as the scheduler reports it: how many "
           "jobs are queued, running, done or broken, and the root cause if "
           "something failed. `name` may be \"all\" for every run."),
        _c("inspect_jobs", ("name", "failed"), ("name",), READS,
           "every individual job inside a run and its state, grouped by step. "
           "`failed` true narrows it to the ones that did not complete."),
        _c("diagnose_run", ("name", "question"), ("name",), READS,
           "read the logs of a run's failed jobs and explain what went wrong. "
           "Gathers the evidence first, then reasons over it. `question` "
           "narrows the investigation."),
        _c("show_run", ("name",), ("name",), READS,
           "the exact GenPipes command a run is, flag by flag, and what can "
           "still be done to it."),
        _c("list_runs", (), (), READS,
           "the runs that currently need attention or are still going."),
        _c("run_history", (), (), READS,
           "every run ever recorded, including finished and abandoned ones."),
        _c("where", (), (), READS,
           "the directories this session is using: the cluster, the working "
           "directory, the run registry and the checkpoint store."),
    )
}

# Names only, for the parser. gate.py needs to know which calls are capability
# calls without knowing anything about what they do.
NAMES = frozenset(TABLE)


def get(name):
    """The Capability for `name`, or None. Never raises on a bad name."""
    return TABLE.get(str(name or ""))


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
    args = dict(args or {})
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
