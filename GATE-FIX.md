# Fix: the gate must not appear until required slots are filled

Supersedes defects 2 and 3 in `AGENT-FIXES.md` with a tighter diagnosis. The
machinery to do this correctly already exists in the repo — it is just
disconnected from the live path. Do not build a new `RunDraft` or a new
required-slot table; wire up what's here.

## What already exists (verified, do not re-implement)

- `slots.gaps(pipeline, protocol, readset, design, pairs, ...)`
  (`genpipe/slots.py:321`) returns, in the correct order, every slot still
  missing for a given pipeline/protocol — pipeline first, then protocol, then
  readset, then design or pairs *only if the protocol actually needs one*
  (`proto.needs`, set per-protocol in the `PIPELINES` table at
  `slots.py:47`). It already knows `rnaseq/stringtie` needs a design and
  `dnaseq/somatic_ensemble` needs pairs, and that `germline_snv` needs
  neither. This is the required-slot table — it's data, not new code.
- `prep.Preparation` (`genpipe/prep.py:264`) is a small object with a
  `.learn(**facts)` that only ever *sets*, never *unsets*, a slot, plus:
  - `prep.missing(prep, candidates)` → the next `Gap` to ask about, or `None`
    (`prep.py:287`)
  - `prep.ready(prep, candidates)` → `True` once `missing()` is `None` and a
    pipeline is set (`prep.py:309`)
- `cli.py` already creates one `prep.Preparation()` per conversation
  (`cli.py:2120`) and re-creates it on `/new` and once a run reaches the gate
  (`cli.py:2155`, `2208`).
- `fakecluster.py:852` already drives its scripted question sequence off
  `slots.gaps()` — proof this logic behaves correctly today, just not for a
  real conversation.

## The actual gap

1. **`_preparing()` never learns file slots.** `cli.py:2019-2023`:
   ```python
   if state.active:
       if found:
           state.learn(pipeline=found.pipeline, protocol=found.protocol,
                       described=found.described)
       return state, None
   ```
   `found` comes from `prep.goal(line)`, which only extracts pipeline/protocol
   (check `prep.py`'s `goal()` — it does not touch readset/design/pairs).
   Readset and design *are* separately recognised, by `intake.find_files()`
   (`intake.py:77`), but that result is only ever appended to the model's
   context as prose (`intake.brief()`) — never written into `preparation`.
   So `prep.ready()` can never become true from what the user typed; it has
   nothing to check against.

2. **Nobody calls `prep.ready()` or `prep.missing()` in the live path.**
   `grep -n "prep\.ready\|prep\.missing" genpipe/cli.py genpipe/agent.py`
   returns nothing. The two functions that exist specifically to answer "is
   this run allowed to reach the gate yet" are dead code outside of
   `fakecluster.py` and the tests that exercise it directly.

3. **The gate itself has no backstop.** Even if (1) and (2) were fixed,
   nothing stops the *model* from writing a submission block that skips the
   conversation's `preparation` state entirely — it decides on its own when
   to submit (`ASK_PROTOCOL`, `agent.py:230`). `gate.build_proposal()`
   (`gate.py:473`) parses whatever the model wrote and shows it, full stop —
   no comparison against `slots.gaps()`.

Two independent enforcement points are needed: the conversational one (stop
asking once `ready()`, keep asking in order until then) and the gate
backstop (refuse `/approve` regardless of how the model got there). Both are
thin wrappers around code that already exists.

## Work

### A. Make `_preparing()` learn every slot, not just two

In `cli.py:2019-2023` (and the `state.learn(...)` call at `2038-2040`), also
learn `readset`, `design`, `pairs` from the line. Reuse `intake.read(line)`
— it already extracts these three by filename-role heuristics
(`intake.py:98`) — and call `.exists()` against the working directory the
same way `intake._resolves()` does, so a named-but-absent file does not get
silently accepted as "learned".

```python
parsed = intake.read(line)
facts = {k: v for k, v in parsed.items() if k in ("readset", "design", "pairs")}
state.learn(pipeline=found.pipeline if found else None,
            protocol=found.protocol if found else None,
            described=found.described if found else None,
            **facts)
```

`.learn()` already no-ops on falsy values, so passing `None`s from a line
that named nothing is safe.

### B. Drive the question loop off `prep.ready()` / `prep.missing()`

Where `_preparing()` currently returns `(state, extra)` with `extra` only
ever a hand-written prose block (`cli.py:2057-2070`), branch on
`prep.ready(state, candidates)`:

- **Not ready** — call `prep.missing(state, candidates)` to get the next
  `Gap` in order (pipeline → protocol → readset → design/pairs, per
  `slots.gaps()`'s own ordering) and surface exactly that one thing. This
  replaces the free-form "ask for it" instruction at `cli.py:2065-2067` with
  the same panel machinery `agent.py`'s `ask_user` node already renders
  (`slots.as_data(gap)` → `interrupt()`), so the question comes from the
  table, not from the model improvising.
- **Ready** — say nothing more about missing slots. Keep the existing
  instruction at `cli.py:2068-2070` that tells the model steps and cluster
  ini are defaults, not questions — that line is already correct, don't
  touch it. This is where "essential vs. extra" already lives in your
  prompt; it just currently fires regardless of whether the essentials were
  actually filled.

`candidates` here is `intake.candidates(...)` — after the discovery-boundary
fix in `AGENT-FIXES.md` defect 1, scoped to the established `project_dir`,
not `os.getcwd()`. If you haven't done that fix yet, do it as part of this
work rather than wiring the readiness check to a directory listing you know
is wrong — a "readset found" candidate pulled from the agent's own repo
would make `ready()` misfire the same way the phantom `design.tsv` did.

### C. Give the gate its own backstop, independent of (A) and (B)

In `gate.build_proposal()` (`gate.py:473`), after `pipeline`, `protocol`,
`design`, `pairs`, `readset` are parsed from the command text (lines
483-489), add:

```python
missing = [g.slot for g in slots.gaps(
    pipeline=pipeline, protocol=protocol,
    readset=readset, design=design, pairs=pairs)]
```

(`slots` here is `genpipe/slots.py` — `gate.py` will need the import.)
Include it in the returned dict: `"missing": missing`.

In `mirror.py`, the only currently-special-cased absence is `output`
(`mirror.py:342`, `seen.setdefault("output", _absent("output"))`, with the
comment explaining every *other* absence "means the run does not use it").
That reasoning holds for `-p` on a germline run; it does not hold for a slot
named in `missing`. Extend the same pattern:

```python
for slot in proposal.get("missing", ()):
    seen.setdefault(slot, _absent(slot, note="required, not set"))
```

Call this after the `output` line so `output`'s existing wording is
unchanged, and pass `proposal["missing"]` into whichever of `mirror.read()`
/ `mirror.from_slots()` ends up building the box (`gate.build_proposal`
returns the dict both draw from).

In `cli.py:_cmd_approve` (`cli.py:550`), refuse before calling
`agent.resume`:

```python
record = agent.registry.get(args[0])  # however the held proposal is fetched
if (record.get("proposal") or {}).get("missing"):
    display.problem(f"missing: {', '.join(record['proposal']['missing'])} "
                     f"— /modify {args[0]} to add")
    return
```

Match whatever accessor `_cmd_approve` already uses to reach the held
proposal (check how `/modify`'s handler in `modify.py` reads it, and reuse
that path exactly rather than inventing a second one) — the point is this
check runs on the stored proposal, so it doesn't matter whether (A)/(B) were
bypassed to get here.

### D. Esc must not mean "pick a default and continue"

Not required for the gate fix itself, but sits right next to it and will be
hit immediately during testing: `agent.py:486-489` currently turns a
declined question into "choose a sensible default... do not ask again",
which is why esc on a protocol panel today still produces a filled-in gate.
Once (A)–(C) are in place, esc during preparation should abandon the run
being prepared, not hand the model license to fill in a required slot on
its own. Distinguish "user pressed esc" from "user typed an explicit
'you choose'" in the payload `ask_user` receives, and only default on the
latter.

## Order

A → B → C, then D. C (the gate backstop) is safe to write first if you want
defense-in-depth immediately — it doesn't depend on A/B — but A and B are
what actually stops the interrogation from firing on things already
answered, which is the complaint that started this.

## Tests

- `tests/test_prep.py` already exercises `prep.ready()`/`prep.missing()` in
  isolation — extend it with cases where a line supplies pipeline, protocol,
  readset and design together and assert `ready()` is `True` with zero
  follow-up questions.
- `tests/test_gate.py` — add: a proposal missing `readset` produces a
  non-empty `missing` list from `build_proposal`; `/approve` on it (via
  whatever harness `test_agent_gate.py` uses to drive `_cmd_approve`) is
  refused and nothing is written as approved in the registry.
- Both belong in the offline suite (`tests/harness.py`, no biomni needed) —
  `slots.gaps()`, `prep.py`, and `gate.build_proposal` are all stdlib-only,
  same as today. Run them, then run the four biomni-dependent suites in
  `~/scratch/biomni-venv` before calling this done, per usual.
