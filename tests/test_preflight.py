"""The environment checks: what blocks a submission and what merely warns.

The distinction is the whole point of the module, so most of these assert on
severity rather than on message text. A check that correctly spots a problem and
then blocks the wrong thing is worse than no check: it either stops work that
would have succeeded, or trains the operator to click past the one warning that
mattered.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from genpipe import preflight
from harness import Report

r = Report("preflight")

# --------------------------------------------------------------------------
r.section("RAP_ID blocks, because without it nothing runs at all")

for good in ("rrg-bourqueg-ad", "def-bourqueg", "ctb-someone-01",
             "rrg-bourqueg-ad_cpu", "RRG-Bourqueg-AD"):
    r.truthy(f"accepts {good!r}", preflight.check_rap_id(good) is None)

for bad, why in [
    ("", "empty"),
    (None, "unset"),
    ("   ", "whitespace only"),
    ("bourqueg", "no allocation prefix"),
    ("my-account", "prefix is not an allocation class"),
    ("-rrg-x", "leading dash"),
]:
    finding = preflight.check_rap_id(bad)
    r.truthy(f"rejects {why}", finding is not None)
    if finding:
        r.equal(f"{why} blocks", finding.severity, preflight.BLOCK)
        r.truthy(f"{why} comes with a fix line", bool(finding.fix))

# --------------------------------------------------------------------------
r.section("JOB_MAIL warns and never blocks")

r.truthy("accepts a normal address",
         preflight.check_job_mail("someone@mcgill.ca") is None)
r.truthy("accepts plus-addressing and subdomains",
         preflight.check_job_mail("a.b+tag@sub.example.org") is None)

for bad, why in [("", "unset"), (None, "missing"), ("notanemail", "no @"),
                 ("a@b", "no dot"), ("a@b.c", "one-letter tld")]:
    finding = preflight.check_job_mail(bad)
    r.truthy(f"flags {why}", finding is not None)
    if finding:
        r.equal(f"{why} only warns", finding.severity, preflight.WARN)

# The case this check exists for: structurally valid, silently bouncing.
typo = preflight.check_job_mail("pbourquejob@gmail.coma")
r.truthy("catches gmail.coma", typo is not None)
r.equal("the typo warns rather than blocks", typo.severity, preflight.WARN)
r.contains("and suggests the right domain", typo.fix, "@gmail.com")
r.truthy("the suggestion is not the typo again", not typo.fix.endswith("coma"))

r.truthy("the real domain is not flagged as a near-miss of itself",
         preflight.check_job_mail("x@gmail.com") is None)
r.truthy("an unrelated real domain passes",
         preflight.check_job_mail("x@physics.ubc.ca") is None)

# --------------------------------------------------------------------------
r.section("Levenshtein, since the typo check rests on it")

r.equal("identical", preflight._distance("gmail.com", "gmail.com"), 0)
r.equal("one insertion", preflight._distance("gmail.coma", "gmail.com"), 1)
r.equal("a transposition", preflight._distance("gmial.com", "gmail.com"), 2)
r.equal("against empty", preflight._distance("", "abc"), 3)

# --------------------------------------------------------------------------
r.section("check() sorts blockers first and separates cleanly")

env = {"RAP_ID": "", "JOB_MAIL": "bad"}
found = preflight.check(env)
r.equal("both problems reported", len(found), 2)
r.equal("the blocker sorts first", found[0].severity, preflight.BLOCK)
r.equal("one blocker", len(preflight.blockers(env)), 1)
r.equal("one warning", len(preflight.warnings(env)), 1)

clean = {"RAP_ID": "rrg-bourqueg-ad", "JOB_MAIL": "someone@mcgill.ca"}
r.equal("a sound environment reports nothing", preflight.check(clean), [])
r.equal("and blocks nothing", preflight.blockers(clean), [])

# A missing variable must read the same as an empty one: os.environ.get returns
# None for the first and "" for the second, and the operator's situation is
# identical.
r.equal("an empty environment reports both", len(preflight.check({})), 2)

sys.exit(r.finish())
