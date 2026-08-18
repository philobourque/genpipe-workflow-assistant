"""Shared test scaffolding: assertions, and a scripted stand-in for the model.

Deliberately not pytest. These suites run on a login node inside a module-loaded
venv, and `python tests/test_runs.py` needs no plugin, no config file and no
network -- which is also exactly what makes them trivial to run in CI. The cost
is about thirty lines of assertion plumbing, kept here so no suite reimplements it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# A RAP_ID that is a shape rather than an allocation. It has to match
# preflight._RAP_PATTERN -- rrg-/def-/ctb-/rpp- -- and it must be obviously not
# real, because the one thing worse than a suite that cannot submit is a suite
# that looks like it is billing somebody.
FAKE_RAP_ID = "rrg-harness-notreal"


def submission_environment():
    """Make this process's environment one a submission may legally happen in.

    CALLED BY NAME, from the four suites that actually approve and submit, and
    by nothing else. That is the whole design of it.

    WHY IT EXISTS. preflight.check_rap_id blocks a submission when RAP_ID is
    unset, because Slurm rejects a job carrying an empty -A and spending an
    approval on one is worse than refusing it. That is correct, and it is not
    what these suites are about: they drive hold -> approve -> submit -> check
    against the fake cluster, and a valid submission environment is part of
    their scenario rather than a thing they are testing.

    They never said so. RAP_ID is exported by every Alliance login shell, so on
    the cluster they inherited a real allocation without anybody choosing to
    give them one, and they passed. Anywhere else -- a laptop, a container, a
    CI runner -- they failed at the moment of approval, with a run that stayed
    held and no obvious reason why. The dependency was real and invisible;
    this makes it explicit and local.

    WHAT IT DELIBERATELY IS NOT:

      not global      it is a function a suite calls, not something that
                      happens on importing this module. A suite that does not
                      submit does not get an allocation, so nothing is
                      quietly granted an environment it never asked for.
      not a default   setdefault, so a real RAP_ID on a cluster is left
                      exactly as it is and these suites go on exercising
                      whatever is really configured there.
      not JOB_MAIL    which warns and never blocks. Leaving it unset keeps the
                      warn-versus-block distinction exercised rather than
                      papered over, and getting those two the right way round
                      is the thing test_preflight is about.

    It cannot mask the checks that prove a missing RAP_ID blocks: those live in
    test_preflight, which calls preflight.check() with explicit env dicts --
    `preflight.check({})` -- and reads os.environ nowhere. Setting a variable
    in this process is invisible to them by construction, and the assertion
    below states that so the property cannot be lost quietly.
    """
    from genpipe import preflight

    os.environ.setdefault("RAP_ID", FAKE_RAP_ID)
    # The environment we just built really is submission-capable...
    assert not preflight.blockers(os.environ), preflight.blockers(os.environ)
    # ...and an empty one still is not, whatever we did to this process.
    assert preflight.blockers({}), "a missing RAP_ID must still block"
    return os.environ["RAP_ID"]


class Report:
    """Counts passes and failures and prints one line per check.

    Every check prints, not just the failures. A suite that is silent when it
    passes gives no evidence it actually ran the thing you just changed.
    """

    def __init__(self, title):
        self.title = title
        self.passed = 0
        self.failed = 0
        print(f"\n=== {title} ===")

    def section(self, label):
        print(f"\n-- {label}")

    def check(self, label, ok, detail=""):
        self.passed += bool(ok)
        self.failed += not ok
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label}" + (f"  {detail}" if detail and not ok else ""))
        return ok

    def equal(self, label, got, want):
        return self.check(label, got == want, f"got={got!r} want={want!r}")

    def truthy(self, label, got):
        return self.check(label, bool(got), f"got={got!r}")

    def contains(self, label, haystack, needle):
        ok = needle in (haystack or "")
        return self.check(label, ok, f"{needle!r} not in {str(haystack)[:200]!r}")

    def finish(self):
        total = self.passed + self.failed
        print(f"\n{self.passed}/{total} passed"
              + (f", {self.failed} FAILED" if self.failed else ""))
        return 0 if self.failed == 0 else 1


# How a scripted model notices it has been rejected: the phrase the gate node
# renders into the transcript when a decision comes back not-approved.
#
# IMPORTED FROM THE PRODUCTION SOURCE rather than retyped, and that is the
# whole point of it having a name. It lives in gate.py because that module is
# stdlib-only: importing it from agent.py would drag biomni into every offline
# suite, which is exactly what these tests exist to avoid. This used to be the literal "was not
# approved" inlined in invoke(), so rewording the message the model actually
# receives silently stopped every rejection test from rejecting -- the scripts
# ran on past the branch they existed to exercise and the suite reported a
# stale value instead of a failure. A fixture that quietly agrees with itself
# is worse than one that breaks loudly.
from genpipe.gate import REJECTION_MARK as _REJECTION_MARK


class ScriptedLLM:
    """Stands in for agent.llm: returns canned responses instead of calling a model.

    Two behaviours beyond a flat script, both needed to test the gate honestly:

      on_reject   a REPLACEMENT script, switched to the first time the
                  conversation contains a rejection. It has to be a whole script
                  rather than a single reply, because a realistic retry is at
                  least two turns -- regenerate, then submit again -- and a
                  one-reply version would leave the retry never reaching the
                  gate, which reads as a product bug when it is a fixture bug.

      sticky      the last entry repeats once a script runs out, so a run that
                  takes one more turn than expected ends instead of raising an
                  IndexError that looks like a product bug.

    `seen` keeps every prompt, so a test can assert the feedback really was
    passed to the model rather than just that the graph looped.
    """

    def __init__(self, script, on_reject=None):
        self.script = list(script)
        self.on_reject = list(on_reject) if on_reject else None
        self.i = 0
        self.calls = 0
        self.seen = []

    def invoke(self, messages):
        from langchain_core.messages import AIMessage

        self.calls += 1
        text = "\n".join(str(getattr(m, "content", "")) for m in messages)
        self.seen.append(text)
        if self.on_reject and _REJECTION_MARK in text:
            self.script, self.on_reject, self.i = self.on_reject, None, 0
        reply = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return AIMessage(content=reply)


def execute_block(*lines):
    """Wrap shell lines in the <execute> shape the agent's parser expects."""
    body = "\n".join(lines)
    return f"<execute>\n#!BASH\n{body}\n</execute>"


def solution(text):
    return f"<solution>{text}</solution>"
