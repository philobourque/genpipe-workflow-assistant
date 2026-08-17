"""Shared test scaffolding: assertions, and a scripted stand-in for the model.

Deliberately not pytest. These suites run on a login node inside a module-loaded
venv, and `python tests/test_runs.py` needs no plugin, no config file and no
network -- which is also exactly what makes them trivial to run in CI. The cost
is about thirty lines of assertion plumbing, kept here so no suite reimplements it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
