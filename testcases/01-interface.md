# Case 1 — the interface, end to end, offline

**Costs nothing. Runs in about two minutes. Run it after any change to the
interface.**

A fake model answers, and a fake GenPipes and Slurm sit on `PATH`. Nothing
reaches a real cluster and nothing spends a token. What this proves is that
every screen still draws, every command still resolves, and the decisions a
person makes still lead where they say they lead.

What it cannot prove: that GenPipes would accept the command. That is case 2.

```bash
./testcases/run.sh 1
```

## Setup

The runner does all of this; it is written down so a failure can be reproduced
by hand.

- a scratch working directory, outside the repository, thrown away afterwards
- `GENPIPE_FAKE=1` and `GENPIPE_FAKE_LLM=1`
- every provider API key stripped from the environment, so a regression that
  reaches for a real model fails loudly instead of quietly costing money
- the app launched on a pty at 44×120, because layout is part of what is
  under test

## Actions

Each row is one action and the one thing that must be observable afterwards.

The runner seeds the working directory with `readset.rnaseq.txt`,
`design.rnaseq.txt` and `pairs.somatic.csv` so the panels have real files to
offer.

| # | action | expected |
|---|---|---|
| 1 | launch | banner draws; `dev mode` line names both fakes |
| 2 | — | no API key prompt appears |
| 3 | `/help` | commands appear grouped: deciding, watching, fixing, setup |
| 4 | `/list` | says there are no runs yet, without an error |
| 5 | type `run rnaseq on the test samples` | the **protocol panel does not appear** — rnaseq defaults to stringtie |
| 6 | — | a **readset panel** appears; answering it is followed by a **design panel**, since stringtie requires one |
| 7 | pick option 1 in each | the chosen filename is carried forward |
| 8 | — | a run name is suggested, derived from the request rather than a timestamp |
| 9 | accept it | the agent generates, and the gate draws with `HOLD` |
| 10 | — | the gate shows the command and the output directory |
| 11 | — | `/approve`, `/modify` and `/reject` are all offered, each with its consequence on the line beneath it |
| 12 | `/modify <name> use steps 1-4 instead` | the run returns to the model rather than submitting, under the same name |
| 13 | — | the redrawn gate's `steps` row reads **1-4**, not the original 1-5 |
| 14 | `/approve <name>` | submission runs; a job list is written |
| 15 | `/list` | the run is listed |
| 16 | `/jobs <name>` | jobs are listed with real states |
| 17 | — | broken jobs and downstream cancellations are counted **separately** |
| 18 |  `/diagnose <name>` | a diagnosis names the failing step |
| 19 | `/where` | prints the real registry and checkpoint paths |
| 20 | type `run dnaseq` | the protocol panel appears with **exactly seven** options |
| 21 | pick `somatic_ensemble` | the readset is asked for first, then a **pairs** panel, because that protocol needs one |
| 22 | press escape in the panel | the panel closes and the app stays alive |
| 23 | reuse an existing run name | the name is redirected, and the earlier run is untouched |
| 24 | paste a multi-line string | it arrives as one line and does not self-submit |
| 25 | `/exit` | prints a farewell, exits with status 0, no traceback |

**Driving the panels: use a keystroke, not a line.** With nine or fewer rows a
digit selects immediately, so sending `1\r` leaks the Enter into whatever prompt
comes next — which is the name prompt, whose suggestion is pre-filled, so the
run silently acquires a name ending in `1`. Both this runner and
`tests/test_app.py` send the bare digit.

## Why these and not others

**5 and 6** are the panel's whole thesis: it must appear exactly when something
is genuinely missing and stay out of the way otherwise. A panel that asks about
rnaseq's protocol is a panel nobody will read by the third run.

**13** is a regression guard with history. The gate once showed the *first*
proposal after a rejection, because the matcher searched the message list
forwards and found the stale one. The operator would have approved a command
they had already rejected. This is the single most dangerous bug the project has
had and it is checked in CI as well as here.

**17** distinguishes "three steps broke" from "three broke and six were
cancelled behind them". A GenPipes DAG cancels everything downstream of a
failure, so counting cancellations as failures inflates the damage and buries
the cause.

**22** matters because a choice panel that cannot be escaped is a worse
interface than no panel. Declining is a legitimate answer and the model can
still ask in prose.

**23** guards a data-loss path. The run name is the LangGraph thread key, and
`AgentState` declares `messages` without a reducer, so reusing a name would
*replace* that thread's state — erasing an earlier conversation and, if it were
parked at the gate, the pending approval with it.

**25** is not a formality. It found a real bug the first time it ran: on the raw
terminal path the interpreter never shut down — it spun at 100% CPU, so `/exit`
and Ctrl+D both appeared to hang and the only way out was to close the terminal.
`tests/test_app.py` had never caught it because its `close()` shuts the pty
master, and the app dies of `SIGHUP` looking perfectly healthy. `main()` now
flushes and calls `os._exit(0)` after the farewell.

## Failure triage

- **Hangs at action 1** — the fake cluster failed to install on `PATH`. On DRAC,
  `BASH_ENV` points at Lmod's init script, which redefines `module` as a shell
  function in every non-interactive bash and bypasses a `PATH` stub.
  `fakecluster.env_for()` unsets every `BASH_FUNC_*` to prevent exactly this.
- **Action 9 never reaches the gate** — the scripted model's reply did not match
  the submission patterns. Check `gate._SUBMIT_PATTERNS`.
- **Assertion found the string in scrollback but not on screen** — expected; the
  runner checks scrollback for "did this ever appear" and the viewport only for
  layout.
- **`SRE module mismatch` on startup** — a shell that has run
  `module load mugqic/genpipes` exports a `PYTHONPATH` pointing at GenPipes'
  Python 3.13 stdlib, and the venv is 3.12. `run.sh` unsets it; if invoking the
  script directly, do the same.
- **An assertion passes that obviously should not** — check it slices
  `app.emitted()` from a mark taken before the action. Against the whole buffer,
  a string printed by an earlier action satisfies the check. Two assertions in
  the first draft of this runner passed that way.
