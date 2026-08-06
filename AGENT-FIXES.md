# Three defects in the run-preparation path

A work order for a coding agent. Every claim below was checked against the
tree at `3e7dcf9` before it was written; the file:line anchors are real and
the "verified" notes say how each was confirmed. Nothing here is a guess
about what the code probably does.

**Scope note.** All three defects are about behaviour under a real model.
`--fake-llm` will not reproduce them, because the stand-in model does not
decide when to read a file, does not choose a default after a declined
question, and does not split generation across turns. Where a fix needs a
test, the test must exercise the deterministic layer (`intake`, `gate`,
`mirror`, `slots`, `cli`) directly rather than assert on a scripted
transcript. Do not close any of these on a green `--fake-llm` run alone.

---

## Defect 1 — The agent explores the filesystem instead of reading the request

### What happens

Given a bare request with no path in it:

```
I want to run an rnaseq pipeline on mouse data
```

the agent runs `hostname`, then `ls -la ~/genpipe-workflow-assistant`, then
`cat design.tsv`, and reports finding a 9-sample WT_vs_Tks5hh design — a file
that has nothing to do with the request. Given a request that *does* name a
location:

```
I would like to run an ampliconseq pipeline, the design file and readset file
are /cvmfs/soft.mugqic/CentOS6/testdata/ampliconseq
```

the path is dropped on the floor and the agent asks "Which readset file?".
Answering with that same directory does not help; it asks again.

### Root cause — three separate things, all verified

**1a. Discovery is rooted at the process's working directory, which is the
agent's own repo.**

- `genpipe/cli.py:2233` — `briefed = intake.brief(line, os.getcwd())`
- `genpipe/agent.py:571` — `found = intake.candidates(os.getcwd())`

`intake.candidates()` (`genpipe/intake.py:121`) lists a directory and buckets
any `.tsv/.txt/.csv` whose stem contains `design`, `readset`, `pairs`. The
repo root contains a real `design.tsv` (91 bytes, 9 samples, committed
Jul 27). `start_agent.sh` does not chdir. So every first turn of every
conversation hands the model `possible design: design.tsv` under the heading
"Files in the working directory that could fill a role", regardless of what
was asked. **This is the phantom `~/genpipe-workflow-assistant/design.tsv`
that appeared in the ampliconseq and dnaseq submission gates.** It was never
inferred from the request; it was on the floor where the agent was standing.

Note both call sites. `brief()` seeds the model's first turn; `_gap_for()`
independently re-scans `os.getcwd()` every time a choice panel is built, so
fixing only one leaves the phantom in the other.

**1b. There is no concept of a project directory.**

`intake.find_files()` (`genpipe/intake.py:77`) iterates whitespace tokens and
`continue`s on anything not ending in `.tsv`, `.txt`, or `.csv`
(`_FILE_SUFFIXES`, line 36). A bare directory path matches nothing and is
discarded silently. `intake.read()` returns exactly five keys — pipeline,
protocol, readset, design, pairs — and there is no `project_dir` anywhere in
the package (`grep -rn project_dir genpipe/` is empty).

So when the user says "they're in `/cvmfs/.../ampliconseq`", the deterministic
layer records nothing, the brief says nothing, and the model is left to
recover the meaning of the path with `ls` and `cat`. It does.

**1c. The prompt instructs it to inspect rather than ask.**

- `genpipe/agent.py:254-255` (ASK_PROTOCOL) — *"Reading a file, listing a
  directory, or running `genpipes <pipeline> --help` is not a question -- do
  that instead."*
- `genpipe/agent.py:199-205` (TALK_PROTOCOL) — *"working with their files IS
  the job when they ask for it. Read a readset, count the samples in it,
  check a column..."*

The model is not disobeying. It is doing what 1a made available and 1c told
it to do. Any fix that only edits the prompt will fail, because 1a will keep
handing it an unrelated file to be curious about.

### On the privacy question

Checked against the transcripts: it read `myReadset.tsv`, `0_annotation.csv`,
and listed `raw_reads/` filenames. It did not decompress or read FASTQ
payloads. So the raw biological data was not opened — but sample identifiers
and group assignments were, unprompted, and that is still the wrong default
for a tool whose value proposition is that it works from paths.

Adopt this as the access policy, and make it explicit in the prompt:

| Level | Permitted |
|---|---|
| nothing | request names no path |
| names only | one non-recursive listing of a directory **the user named** |
| metadata | header line, column names, row count — after a candidate is chosen |
| contents | never automatic; only on explicit request |
| FASTQ/BAM/CRAM payload | never |

### Work

1. Add `project_dir` to the intake vocabulary. `intake.read()` should
   recognise a token that is an existing directory and return it under a new
   `project_dir` key. Keep the parser's existing conservatism: an existing
   directory is a fact, a non-existent path is not a guess to make.
2. Thread it through as session state so it survives turns. It must be
   settable explicitly too — add `/project <dir>`, following the command
   table at `genpipe/cli.py:1920`.
3. Replace `intake.candidates(os.getcwd())` at **both** `cli.py:2233` and
   `agent.py:571` with the established `project_dir`. When no project
   directory has been established, **discover nothing** — do not fall back to
   the cwd. An empty candidate list is correct; a candidate from the agent's
   own repo is a fabrication.
4. Once `project_dir` is known, `brief()` may include one non-recursive
   listing of immediate names. Names only. Not contents.
5. Rewrite ASK_PROTOCOL:254-255 and the file-handling paragraph of
   TALK_PROTOCOL to state the table above. Keep "reading a file is not a
   question" only for a path the user explicitly attached to *this* run.

### Acceptance

- With an unrelated `design.tsv` in the cwd and the request "I want to run an
  rnaseq pipeline on mouse data": no shell command runs, `design.tsv` is not
  mentioned, and the reply asks for a project directory or readset.
- "the readset and design are in `/some/dir`" populates `project_dir`
  deterministically, before the model is called.
- Given a project directory, immediate filenames may be listed; no `cat`, no
  recursive walk into `raw_reads/`.
- Given an explicit readset path, existence may be checked; contents are not
  displayed.
- "Hi" produces a `<solution>` and no execution. (This already holds — keep it
  holding.)

---

## Defect 2 — The gate can propose a run that is missing required arguments

### What happens

Two observed shapes, both approvable:

- an ampliconseq gate showing only `name` and `output`, with no pipeline
  arguments at all;
- a dnaseq gate showing `protocol germline_snv`, `steps 1-5`, and the phantom
  `design ~/genpipe-workflow-assistant/design.tsv` — **and no readset row at
  all**, for a run that cannot start without `-r`.

`/approve <name>` on either submits `bash cmd.sh` to Slurm.

### Root cause — verified

**2a. `build_proposal` has no notion of a required slot.**
`genpipe/gate.py:473-529`. Every field is parsed with `flag_value()` and every
one is appended to the explanation only `if` truthy. There is no required
set, no validation, no error return. A proposal in which every slot is `None`
is a well-formed proposal.

**2b. The mirror cannot show an absence it was never given.**
`mirror.read()` (`genpipe/mirror.py:292`) tokenises the generated command and
emits one row per flag *present*. A missing `-r` produces no row. Exactly one
absence is special-cased — `seen.setdefault("output", _absent("output"))` at
`mirror.py:342` — with a comment explaining that every other missing flag
"means the run does not use it". That reasoning is sound for `-p` on a
germline run and wrong for `-r` on any run. So the screen the user approves
is silent about the one argument that is always required.

**2c. `/approve` validates nothing.**
`genpipe/cli.py:550-565`. It checks that an argument was typed, then calls
`agent.resume(name, approved=True)`. There is no completeness check between
the keystroke and the scheduler.

**2d. A declined question becomes a silent default.**
`genpipe/agent.py:486-489`: when the user presses esc, the observation fed
back is *"The user declined to answer. Choose a sensible default, state which
one you chose, and carry on -- do not ask again."* That is why esc on the
dnaseq protocol panel produced a gate with `germline_snv` rather than
stopping. Esc currently means "pick for me", and the user expects it to mean
"leave this run alone".

### Work

1. Define required slots as data, per pipeline and protocol, next to the
   existing tables in `genpipe/slots.py`. `-r` is required by every pipeline;
   `-d` is required by the protocols that already declare it (the RNA-seq
   `stringtie` differential-expression path); `-p` is required by the somatic
   protocols. Derive from what is already encoded in `slots.py` rather than
   introducing a second source of truth.
2. Have `build_proposal` compute and return a `missing` list.
3. Make the mirror render a required-but-absent row explicitly, in the style
   `_absent()` already establishes — `readset  -r  not set — required`. Widen
   the special case at `mirror.py:342` from "output only" to "output, plus
   anything in `missing`".
4. Block approval. If `missing` is non-empty the gate must not present
   `/approve` as available, and `_cmd_approve` must refuse with a message
   naming what is absent. Keep the refusal in `cli.py` even after the gate
   hides the affordance — the gate is a display and the guard belongs on the
   path to Slurm.
5. Change esc semantics. Esc on a question panel should abandon the run in
   preparation, not hand the model a licence to default. Distinguish it from
   an explicit "you choose" answer, which may keep the current behaviour.

### Acceptance

- A proposal with no `-r` cannot be approved; `/approve` on it prints what is
  missing and nothing reaches the scheduler.
- The gate box shows a `readset -r not set — required` row rather than
  omitting the line.
- Esc during preparation leaves no held run behind.
- `tests/test_gate.py` gains cases for each. It is the suite whose failure
  "means something unsafe rather than something broken"
  (`.github/workflows/tests.yml`) and it runs first in CI — these belong there.

---

## Defect 3 — Questions are asked in no particular order, and are asked at all when the request already answered them

### What happens

A request that supplies pipeline, both file locations, and intent still walks
through a panel sequence and still arrives at a gate missing the things that
were supplied. Meanwhile a request that supplies nothing reaches a gate too.
There is no relationship between how complete the request was and how much
the agent asks.

### Root cause

The model decides *when* to ask, one question at a time
(`ASK_PROTOCOL`, `genpipe/agent.py:230-263`), with no representation of what
the run still needs. `intake.brief()` marks facts "Already settled by the
request above -- do not ask again", which is a prompt-level request, not an
enforcement. There is no structure that holds the run being assembled, so
nothing can compute "what is still missing, in what order, and is that list
now empty".

There is also an unresolved instruction conflict about planning, which is
worth fixing while in here but is **not** the cause of the slowness:

- `biomni/agent/a1.py:1058-1075` (inherited via `super().configure()` at
  `genpipe/agent.py:393`) — *"make a plan first... Format your plan as a
  checklist... Always show the updated plan after each step so the user can
  track progress."*
- `genpipe/agent.py:208` (TALK_PROTOCOL) — *"Do not write numbered checklists
  or restate your plan."*

Both are in the same system prompt. Resolve it with one unambiguous rule
rather than by restoring the visible checklist — a visible checklist would add
generated tokens per turn and make the interaction slower, not faster.

### On the slowness

The perceived slowness is round trips, and the diagnosis holds up: Biomni's
loop is generate → execute → generate, and the graph rebuilt at
`genpipe/agent.py:514-531` preserves that shape (`execute → generate` edge,
line 528). So `hostname`, `ls`, `cat`, `cat` is four model calls before any
work starts. The tool retriever is already off (`cli.py:432`,
`use_tool_retriever = False`), so it is not the source. **Fixing Defect 1
removes most of these round trips**, because the round trips exist to recover
information the deterministic layer threw away. Treat the slowness as a
symptom of Defect 1, not as separate work.

### Work

1. Introduce a `RunDraft` — one structure holding pipeline, protocol,
   project_dir, readset, design, pairs, output_dir, command_file, steps —
   owned by the session and updated by `intake` before the model is called.
   `genpipe/modify.py:59` already defines a `ROWS` tuple and
   `modify.py:70` a `FILL_ORDER`; reuse them rather than inventing a second
   field order, so the draft, the gate mirror, and `/modify` agree.
2. Compute the missing set from the draft against the required-slot table
   from Defect 2. Ask in `FILL_ORDER`, essentials before extras.
3. When the missing set is empty on arrival, do not ask anything — go
   straight to generation and the gate. This is the "skip the questions when
   the request was clear" behaviour, and it falls out of 1 and 2 for free.
4. Apply defaults for extras only, and say which default was applied. Steps
   default to all steps unless the user narrowed them.
5. Resolve the planning conflict: either strip the checklist paragraph from
   the inherited prompt after `super().configure()` (the same
   search-and-replace technique `address_user()` already uses at
   `genpipe/agent.py:541-547`) or override it with a single rule — *maintain
   the run as structured state; do not print a checklist unless asked.*

### Acceptance

- A request naming pipeline, protocol, readset and design asks **zero**
  questions and goes straight to the gate.
- A request naming only the pipeline asks for the required slots in
  `FILL_ORDER`, one at a time, and never asks about a slot the request
  already filled.
- The draft's contents and the gate mirror's rows never disagree.
- Exactly one planning instruction is present in the assembled system prompt.
  Assert on `agent.system_prompt` — that the checklist text is absent — rather
  than on model behaviour.

---

## Order

Do them in this order; the dependencies are real.

1. **Defect 1**, discovery boundary. It is the source of the phantom
   `design.tsv` in both gate screenshots, and of most of the round trips
   behind the slowness. Nothing else is worth measuring until the agent stops
   reading the wrong directory.
2. **Defect 2**, the approval guard. Independent of 1, and it is the one whose
   failure mode ends at Slurm.
3. **Defect 3**, the draft and the ask order. Wants the required-slot table
   from 2 and the `project_dir` from 1, so it goes last.

## Testing

Offline suites, each standalone, no pytest (`tests/harness.py`):

```
python tests/test_gate.py       # first — the safety suite
python tests/test_intake.py
python tests/test_display.py
...
```

The four that drive the real LangGraph gate and a real terminal —
`test_lifecycle.py`, `test_app.py`, `test_agent_gate.py`,
`test_mock_pipeline.py` — need `biomni==0.0.8` and run in
`~/scratch/biomni-venv` on the cluster. Run both sets and report real output
before handing back. New coverage belongs in `test_intake.py` (project_dir,
no-cwd-fallback), `test_gate.py` (required slots, approval refusal), and
`test_modify.py` / a new draft suite (fill order).
