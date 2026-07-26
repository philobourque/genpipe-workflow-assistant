#!/usr/bin/env python3
"""Case 2: a real run on the real cluster, using GenPipes' own CIT data.

Real model, real GenPipes, real Slurm, real allocation. The run is short because
the data is chr19 and cit.ini caps the walltimes -- not because any step was
skipped or stubbed. Every code path production uses is the code path this takes.

The action numbering matches the table in 02-cluster.md.

Preconditions are checked before anything is spent, and a missing one stops the
case with a named cause rather than a failure three minutes later that has to be
traced back.
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import preflight                                # noqa: E402
import runs as runs_store                       # noqa: E402
from harness import Report                      # noqa: E402

MODULE = "mugqic/genpipes/6.1.1"
POLL_SECONDS = 30
GIVE_UP_AFTER = 45 * 60


def shell(command, timeout=600):
    """Run one command through a login-shell-ish bash, module load included.

    BASH_ENV is cleared for the child: on DRAC it points at Lmod's init script,
    which redefines `module` as a shell function in every non-interactive bash.
    Leaving it set makes the environment this case runs in differ from the one
    the app runs in, which is the one thing a cluster test must not do.
    """
    env = dict(os.environ)
    env.pop("BASH_ENV", None)
    return subprocess.run(["bash", "-lc", command], capture_output=True,
                          text=True, timeout=timeout, env=env)


def preconditions(r):
    """Everything that must be true before this case is allowed to spend."""
    r.section("preconditions")

    findings = preflight.check()
    blockers = [f for f in findings if f.blocking]
    for finding in findings:
        print(f"      {finding.variable}: {finding.problem}")
    r.check("RAP_ID is a usable allocation", not blockers)

    has_key = any(os.environ.get(v) for v in
                  ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                   "GEMINI_API_KEY", "GROQ_API_KEY"))
    r.check("a real API key is configured", has_key)

    probe = shell(f"module load {MODULE} && genpipes --version")
    r.check("the genpipes module loads", probe.returncode == 0,
            probe.stderr.strip()[:200])

    home = os.environ.get("MUGQIC_INSTALL_HOME", "")
    readset = f"{home}/testdata/rnaseq/readset.rnaseq.txt"
    design = f"{home}/testdata/rnaseq/design.rnaseq.txt"
    r.check("the CIT readset exists", os.path.exists(readset), readset)
    r.check("the CIT design exists", os.path.exists(design), design)

    ini_probe = shell(f"module load {MODULE} && ls $GENPIPES_INIS/rnaseq/cit.ini")
    r.check("rnaseq cit.ini is on the install", ini_probe.returncode == 0)

    return r.failed == 0, readset, design


def generate(r, workdir, readset, design, steps, pipeline="rnaseq",
             production=False):
    """Actions 3-7: generation, and what the generated script actually says.

    The production path is the same function with the CIT overlay removed, so
    case 3 exercises this code rather than a second copy of it that drifted.
    """
    r.section("generation")
    script = os.path.join(workdir, "cmd.sh")
    # cit.ini LAST: it is an override layer and has to win over rorqual.ini's
    # production walltimes. If this order is wrong the run still generates and
    # still submits -- it just quietly uses production resources, which is
    # exactly the class of error this whole case exists to catch.
    stack = [f"$GENPIPES_INIS/{pipeline}/{pipeline}.base.ini",
             "$GENPIPES_INIS/common_ini/rorqual.ini"]
    if not production:
        stack.append(f"$GENPIPES_INIS/{pipeline}/cit.ini")

    design_flag = f"-d {design} " if design else ""
    command = (
        f"module load {MODULE} && cd {workdir} && genpipes {pipeline} "
        f"-c {' '.join(stack)} "
        f"-r {readset} {design_flag}-s {steps} -o {workdir} -g {script}"
    )
    result = shell(command)
    r.check("5  generation succeeds", result.returncode == 0,
            result.stderr.strip()[-400:])
    r.check("5  cmd.sh was written", os.path.exists(script))
    if not os.path.exists(script):
        return None, command

    body = open(script).read()
    r.check("7  sbatch lines carry the allocation", "-A " in body)
    short = any(w in body for w in ("0:10:00", "0:15:00", "0:20:00"))
    if production:
        # The inverse assertion, and it is the one that matters for case 3:
        # a production run that quietly inherited CIT's ten-minute walltimes
        # would be killed mid-step and look like a resource problem.
        r.check("7  production walltimes are in force, not CIT's", not short,
                "found a CIT-length walltime in a production run")
        r.check("7  no CIT overlay in the command", "cit.ini" not in command)
    else:
        # The CIT walltime is what proves the override layer landed. A
        # production rnaseq step asks for hours; cit.ini caps most at ten
        # minutes.
        r.check("7  CIT walltimes are in force, not production ones", short,
                "no short walltime found -- did cit.ini land last in -c?")
    return script, command


def submit(r, workdir, script):
    """Action 8-9: the consequential act, and the artifact it leaves."""
    r.section("submission")
    result = shell(f"cd {workdir} && bash {script}", timeout=900)
    r.check("8  submission returned cleanly", result.returncode == 0,
            result.stderr.strip()[-400:])

    job_dir = os.path.join(workdir, "job_output")
    listing = []
    if os.path.isdir(job_dir):
        listing = [os.path.join(job_dir, n) for n in os.listdir(job_dir)
                   if ".job_list." in n]
    r.check("9  a job list was written", bool(listing), f"looked in {job_dir}")
    return sorted(listing)[-1] if listing else None


def monitor(r, job_list):
    """Actions 11-12: real states, from sacct, until they stop changing."""
    r.section("monitoring")
    jobs = runs_store.parse_job_list(job_list)
    r.check("11 the job list parses", bool(jobs), f"got {len(jobs)} jobs")
    if not jobs:
        return {}

    ids = [j.jid for j in jobs]
    print(f"      {len(ids)} jobs submitted")
    seen, deadline = {}, time.time() + GIVE_UP_AFTER
    states = {}
    while time.time() < deadline:
        states = runs_store.query_states(ids)
        for jid, state in states.items():
            seen.setdefault(jid, []).append(state)
        active = [s for s in states.values()
                  if s in ("PENDING", "RUNNING", "REQUEUED", "")]
        done = len(states) - len(active)
        print(f"      {done}/{len(ids)} settled", flush=True)
        if not active:
            break
        time.sleep(POLL_SECONDS)

    r.check("11 sacct returned states for the run's own job ids",
            len(states) > 0)
    moved = [j for j, hist in seen.items() if len(set(hist)) > 1]
    r.check("11 at least one job was observed changing state", bool(moved),
            "every job was already terminal on the first poll -- possible, "
            "but usually means the poll started too late")

    bad = {j: s for j, s in states.items() if s in runs_store.BAD_STATES}
    r.check("12 every job completed", not bad, f"not ok: {bad}")
    return states


def report(r, workdir, job_list):
    """Actions 13-14: the tools agree, and the outputs exist."""
    r.section("the run's own report")
    result = shell(f"module load {MODULE} && cd {workdir} && "
                   f"genpipes tools log_report --loglevel ERROR "
                   f"--tsv {workdir}/log.out {job_list}")
    r.check("13 log_report runs against the job list", result.returncode == 0,
            result.stderr.strip()[-300:])
    parsed = runs_store.parse_log_report(result.stdout)
    r.check("13 and reports per-step counts", bool(parsed.get("counts")),
            f"got={parsed}")
    r.check("14 a report directory exists",
            os.path.isdir(os.path.join(workdir, "report")))
    return parsed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", default=os.path.expanduser("~/scratch/case2"))
    ap.add_argument("--steps", default="1-4",
                    help="CIT step range; keep it small (default 1-4)")
    ap.add_argument("--skip-wait", action="store_true",
                    help="submit but do not wait for completion")
    ap.add_argument("--production", action="store_true",
                    help="case 3: no cit.ini, real data, full step range")
    ap.add_argument("--pipeline", default="rnaseq")
    ap.add_argument("--readset", help="required with --production")
    ap.add_argument("--design")
    ap.add_argument("--confirm", action="store_true",
                    help="accepted and ignored here; run.sh checks it")
    args = ap.parse_args()

    title = ("case 3 -- production" if args.production
             else "case 2 -- a real run on the real cluster")
    r = Report(title)
    ok, readset, design = preconditions(r)

    if args.production:
        # Real data is the operator's to name. Defaulting to CIT data under a
        # production flag would produce a green result that proved nothing.
        if not args.readset:
            print("\n--production requires --readset (and --design if the "
                  "protocol takes one). Nothing was submitted.")
            return r.finish()
        readset, design = args.readset, args.design
        r.check("the production readset exists", os.path.exists(readset), readset)
        ok = ok and os.path.exists(readset)
        if args.steps == "1-4":
            print("\nRefusing to run production with the default 1-4 step "
                  "range. Pass --steps explicitly, from --help.")
            return r.finish()

    if not ok:
        print("\nPreconditions failed. Nothing was submitted.")
        return r.finish()

    workdir = os.path.join(args.workdir, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(workdir, exist_ok=True)
    print(f"\n      working in {workdir}")

    started = time.time()
    script, command = generate(r, workdir, readset, design, args.steps,
                               pipeline=args.pipeline,
                               production=args.production)
    record = {"workdir": workdir, "command": command, "steps": args.steps,
              "production": args.production, "pipeline": args.pipeline}

    if script and not args.skip_wait:
        job_list = submit(r, workdir, script)
        record["job_list"] = job_list
        if job_list:
            states = monitor(r, job_list)
            record["final_states"] = states
            report(r, workdir, job_list)

    record["seconds"] = round(time.time() - started, 1)
    out = os.path.join(HERE, "last-run-2.json")
    with open(out, "w") as fh:
        json.dump(record, fh, indent=2, default=str)
    print(f"\n      recorded to {out}")
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
