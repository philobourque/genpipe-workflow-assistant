"""Environment checks that run before anything reaches the scheduler.

GenPipes does not validate the environment it inherits. `common_ini/rorqual.ini`
interpolates two shell variables straight into every generated sbatch line:

    cluster_other_arg = --mail-type=END,FAIL --mail-user=$JOB_MAIL -A $RAP_ID

so an unset RAP_ID produces `-A` with nothing after it and every job in the run
is rejected at submit time -- after generation, after the operator read the
proposal, after they approved it. The gate would have done its job and the run
would still die. A wrong JOB_MAIL costs only notifications, and costs them
silently: mail bounces and nothing anywhere says so.

Those two failures deserve different answers, which is the whole reason this
module grades its findings rather than returning a bool. RAP_ID hard-blocks
submission, so the gate refuses to offer approval and says why. JOB_MAIL does
not, so it warns once at startup and gets out of the way. Blocking on a
notification address would train the operator to ignore the check that matters.

Standard library only, like gate_rules and runs, so CI can verify it in a second
without a cluster or an agent stack.
"""

import os
import re

BLOCK = "block"
WARN = "warn"

# Alliance allocation prefixes. rrg- is a Resource Allocation Competition award,
# def- the default allocation every PI gets, ctb- contributed, rpp- a research
# platform. Slurm reports these with a _cpu or _gpu suffix appended
# (rrg-bourqueg-ad_cpu) while .bash_profile normally carries the bare form, so
# both are accepted.
_RAP_PATTERN = re.compile(r"^(rrg|def|ctb|rpp)-[a-z0-9][a-z0-9_-]*$", re.I)

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@([^@\s.]+\.)+[a-z]{2,24}$", re.I)

# Checked by edit distance, not equality. A TLD allowlist would be endless and
# would still miss the case this is here for: JOB_MAIL=...@gmail.coma, which is
# structurally a valid address and had been bouncing every job notification for
# months. One transposed or trailing character off a domain this common is a
# typo, not a domain.
_COMMON_DOMAINS = (
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "icloud.com",
    "protonmail.com", "mail.mcgill.ca", "mcgill.ca", "umontreal.ca",
    "computationalgenomics.ca",
)


class Finding:
    """One thing wrong with the environment.

    severity is BLOCK or WARN; `fix` is the literal line to put in
    ~/.bash_profile, because a check that reports a problem without the remedy
    just relocates the work.
    """

    __slots__ = ("variable", "severity", "problem", "fix")

    def __init__(self, variable, severity, problem, fix):
        self.variable = variable
        self.severity = severity
        self.problem = problem
        self.fix = fix

    @property
    def blocking(self):
        return self.severity == BLOCK

    def __repr__(self):
        return f"<Finding {self.variable} {self.severity}: {self.problem}>"

    def __eq__(self, other):
        return (isinstance(other, Finding)
                and (self.variable, self.severity, self.problem, self.fix)
                == (other.variable, other.severity, other.problem, other.fix))


def _distance(a, b):
    """Levenshtein distance, iterative two-row form.

    Only ever called on short domain strings against a fixed list, so the
    quadratic cost is irrelevant and a dependency would not be worth it.
    """
    if a == b:
        return 0
    if not a or not b:
        return len(a) or len(b)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1,        # deletion
                               current[j - 1] + 1,     # insertion
                               previous[j - 1] + (ca != cb)))  # substitution
        previous = current
    return previous[-1]


def check_rap_id(value):
    """The allocation jobs are billed to. Blocking: without it nothing runs."""
    if not value or not value.strip():
        return Finding(
            "RAP_ID", BLOCK,
            "not set -- every job would be submitted with an empty -A and "
            "rejected by the scheduler",
            "export RAP_ID=rrg-yourgroup-ab",
        )
    value = value.strip()
    if not _RAP_PATTERN.match(value):
        return Finding(
            "RAP_ID", BLOCK,
            f"{value!r} is not an allocation name -- expected one starting "
            "rrg-, def-, ctb- or rpp-",
            "export RAP_ID=rrg-yourgroup-ab",
        )
    return None


def check_job_mail(value):
    """The notification address. Never blocking: it cannot stop a job running,
    only stop you hearing about it."""
    if not value or not value.strip():
        return Finding(
            "JOB_MAIL", WARN,
            "not set -- jobs will run, but no completion or failure mail "
            "will reach you",
            "export JOB_MAIL=you@example.com",
        )
    value = value.strip()
    if not _EMAIL_PATTERN.match(value):
        return Finding(
            "JOB_MAIL", WARN,
            f"{value!r} is not a valid address -- job mail will bounce",
            f"export JOB_MAIL=you@example.com   # currently {value}",
        )
    domain = value.rsplit("@", 1)[1].lower()
    for known in _COMMON_DOMAINS:
        if domain != known and _distance(domain, known) == 1:
            return Finding(
                "JOB_MAIL", WARN,
                f"domain {domain!r} is one character off {known!r} -- "
                "job mail is probably bouncing",
                f"export JOB_MAIL={value.rsplit('@', 1)[0]}@{known}",
            )
    return None


def check(env=None):
    """Every finding, worst first. Empty list means the environment is sound."""
    env = os.environ if env is None else env
    found = [check_rap_id(env.get("RAP_ID")),
             check_job_mail(env.get("JOB_MAIL"))]
    found = [f for f in found if f is not None]
    found.sort(key=lambda f: f.severity != BLOCK)
    return found


def blockers(env=None):
    """Only the findings that must stop a submission."""
    return [f for f in check(env) if f.blocking]


def warnings(env=None):
    return [f for f in check(env) if not f.blocking]
