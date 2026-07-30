# Test cases

Looking for something to try by hand while changing how the agent behaves? See
[SCENARIOS.md](SCENARIOS.md) — five conversations to hold with it, each the
shortest path to a distinct piece of machinery, with the expected observation for
every line you type.

Four numbered cases, in ascending order of what they cost and what they prove.
Cases 1-3 are scripted; case 4 is hand-driven, because the point of it is the
*questions*, and a script that asked them would be asserting on the model's
prose -- exactly the thing cases 1-3 avoid asserting on.
They are not unit tests — the suites in `tests/` cover the parts. These walk the
whole product the way a person walks it, and they exist so that "does it still
work?" has an answer that does not depend on anyone's memory of what it used to
do.

| | what it exercises | costs | how long | how often |
|---|---|---|---|---|
| [1. Interface](01-interface.md) | every screen, command and decision path | nothing | ~2 min | every change to the interface |
| [2. Cluster](02-cluster.md) | a real run on real Slurm, chr19 data | a few core-hours | ~30 min | before a release, after touching submission or monitoring |
| [3. Production](03-production.md) | the real thing, real data, full steps | real allocation | hours | rarely, and deliberately |
| [4. Mouse RNA-seq](04-mouse-rnaseq.md) | a real research dataset: non-human genome, no design file, days | part A nothing, part B a real allocation | part A ~15 min, part B 1-2 days | part A after touching intake, the ask node, the gate or the grammar; part B before claiming it works on anything but human data |

## Why four and not one

Each case removes one layer of pretence.

Case 1 has a fake model and a fake cluster, so it can assert on exact strings
and run in a loop. It proves the interface is coherent. It cannot prove GenPipes
would accept the command, because nothing here is GenPipes.

Case 2 has a real model and a real cluster. Nothing is stubbed: real generation,
real ini layering, real `sbatch`, real job IDs, real `sacct`. It is short only
because the *data* is one chromosome and the walltimes come from GenPipes' own
`cit.ini`. That distinction matters — a shortened job that took a different code
path would prove nothing about the path production uses.

Case 3 removes the last difference: real data, real genome, full step range. It
is the only one that can catch a problem that appears at scale, and the only one
expensive enough that it needs a reason.

Case 4 removes a difference the other three share with each other: they are all
*human*, and two of them run on data GenPipes ships. That hides three things. The
genome ini rule resolves to a default whose indexes happen to be built, because
they are built for *Homo sapiens*. The CIT testdata ships a design file, so the
ask node never fires. And one chromosome at ten-minute walltimes never outlives
the session that started it, so a stale status costs nothing. Case 4 breaks all
three, and it is split in two so the useful half is free: part A stops at the
HOLD box, which is the gate turning out to be a testing property and not only a
safety one.

## The rule they share

Every case is a **named list of actions with an expected observation for each**.
Not "check it works". If an action's observation cannot be written down before
running it, the case is not ready to be run.

## Running them

```bash
./testcases/run.sh 1                  # interface, offline
./testcases/run.sh 2                  # cluster, CIT data
./testcases/run.sh 3                  # production; refuses without --confirm
```

Case 4 has no `run.sh` entry. Walk it by hand from
[04-mouse-rnaseq.md](04-mouse-rnaseq.md).

Case 1 also runs unattended, which is why it is the one wired into CI's
neighbours rather than the others.
