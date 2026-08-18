#!/usr/bin/env python
"""The three gate verbs, and what each one costs.

The gate used to offer two: approve, and a reject that secretly meant rework --
it regenerated and came back. So there was no way to abandon a run at all, and a
run you had mentally dropped went on appearing in /list and in the startup
pending line forever.

    /approve   submits. Irreversible, and the only one that spends anything.
    /modify    rewrites the command and asks again. What reject used to do.
    /reject    abandons the run. Terminal, nothing submitted, reason kept.

Two properties here are safety properties rather than behaviour, and they are
the reason this file is stdlib-only and runs on every push:

  * prose never approves. "looks good, go ahead" typed at the gate must be
    REFUSED with the command that would work -- not read as consent. Approval is
    typed, always.
  * a rename leaves no ghost. The registry is append-only and get() takes the
    last match, so a rename that rewrote only the current record would leave the
    run reachable under a name /list no longer shows.

Run:  python tests/test_modify.py
"""
import os
import sys
import tempfile

from harness import Report

from genpipe import modify
from genpipe import override
from genpipe import runs


PROPOSAL = {
    "command": "bash cmd.sh",
    "slots": {"pipeline": "chipseq", "protocol": "chipseq", "steps": "1-5",
              "design": "design.tsv", "inis": [], "pairs": None,
              "readset": "readset.tsv", "output_dir": None},
}


def main():
    r = Report("modify")

    # ------------------------------------------------------------------ #
    r.section("prose at the gate never approves")

    for line in ("lgtm", "looks good", "looks good, go ahead", "go ahead",
                 "yes", "ship it!", "ok do it", "approve", "sounds good",
                 "perfect, submit it"):
        r.check(f"refused as approval: {line!r}", modify.is_approval_shaped(line))

    # The other half, and the more important one: a line that merely STARTS
    # agreeably is a change request and must reach the modify path. A prefix
    # match here would have swallowed the change and printed an approve line.
    for line in ("yes, but use steps 1-8", "looks good except the protocol",
                 "use steps 1 through 8", "change -t to atacseq",
                 "ok so what does step 4 do"):
        r.check(f"not an approval: {line!r}", not modify.is_approval_shaped(line))

    # ------------------------------------------------------------------ #
    r.section("tier 1: a wrong protocol is answered with the right ones")

    verdict = modify.check("protocol", "germline_snv", PROPOSAL)
    r.check("rejected", not verdict.ok)
    r.contains("and named as another pipeline's", verdict.message, "dnaseq")
    r.equal("with this pipeline's real protocols offered",
            sorted(o.value for o in verdict.options), ["atacseq", "chipseq"])
    r.check("which never reached a model", True)

    ok = modify.check("protocol", "atacseq", PROPOSAL)
    r.check("a legal one is accepted", ok.ok)
    r.contains("with its consequence stated in the same breath",
               ok.note, "atac")

    # A protocol switch that newly REQUIRES a file has to say so immediately,
    # not two steps later when generation fails on a missing -d.
    rna = {"slots": {"pipeline": "rnaseq", "protocol": "variants",
                     "steps": "1-5"}}
    switch = modify.check("protocol", "stringtie", rna)
    r.check("accepted", switch.ok)
    r.contains("and the new requirement is named", switch.note, "design")

    # ------------------------------------------------------------------ #
    r.section("tier 1: a file that is not on disk is caught before a model call")

    with tempfile.TemporaryDirectory() as tmp:
        missing = modify.check("design", "nope.tsv", PROPOSAL, directory=tmp)
        r.check("rejected", not missing.ok)
        r.contains("and says what would have happened", missing.message,
                   "genpipes would fail")
        real = os.path.join(tmp, "design.tsv")
        open(real, "w").close()
        r.check("a file that is there is accepted",
                modify.check("design", "design.tsv", PROPOSAL, directory=tmp).ok)

    # ------------------------------------------------------------------ #
    r.section("tier 2: form, not meaning")

    for good in ("1-5", "3,6-8", "4", "1-20"):
        r.check(f"well formed: {good}", modify.valid_steps(good))
    for bad in ("one-five", "5-1", "0-3", "1..5", ""):
        r.check(f"malformed: {bad!r}", not modify.valid_steps(bad))

    # ------------------------------------------------------------------ #
    r.section("tier 3: steps are reasoned about against --help, never a table")

    # There is no step table in this repo and there must not be one:
    # genpipes.md says so outright, because the numbered list is version-exact
    # and a copy here would be wrong on the next GenPipes release while looking
    # authoritative. So the help text is an ARGUMENT.
    help_text = "\n".join(
        f"{i}- {name}" for i, name in enumerate(
            ["picard_sam_to_fastq", "trimmomatic", "bwa_mem", "mark_duplicates",
             "haplotype_caller", "metrics", "report"], 1))

    risks, stop = modify.step_risk("1-2,5-7", help_text)
    r.equal("an internal gap is not a hard stop", stop, None)
    r.check("but it is a risk", bool(risks))
    r.contains("naming what is skipped", risks[0], "3-4")
    r.contains("and reasoning rather than asserting", risks[0], "usually")

    risks, stop = modify.step_risk("1-7", help_text)
    r.equal("a complete range raises nothing", risks, [])
    r.equal("and stops nothing", stop, None)

    risks, stop = modify.step_risk("5-9", help_text)
    r.check("a step past the end is a hard stop", stop is not None)
    r.contains("and says what the range really is", stop, "1-7")

    r.equal("with no --help there is no opinion",
            modify.step_risk("1-5", ""), ([], None))

    # ------------------------------------------------------------------ #
    r.section("one sentence, whatever the number of changes")

    text = modify.sentence(PROPOSAL, {"protocol": "atacseq", "steps": "1-8"})
    r.contains("names the flag, not the row", text, "-t")
    r.contains("both sides of the delta", text, "from chipseq to atacseq")
    r.contains("the second change too", text, "1-8")
    # The half that earns its length: a model told only what to change
    # regenerates everything and drifts on a flag nobody asked it to touch.
    r.contains("and what must not move", text, "leave")
    r.contains("-d among them", text, "-d")

    r.equal("a rename alone produces no sentence at all",
            modify.sentence(PROPOSAL, {"name": "better-name"}), "")

    # ------------------------------------------------------------------ #
    r.section("cross-field consequences surface before submission")

    notes = modify.cross_check({"slots": {"pipeline": "rnaseq",
                                          "protocol": "variants"}},
                               {"protocol": "stringtie"})
    r.check("a protocol that now needs a design says so",
            any("design" in n for n in notes))

    notes = modify.cross_check({"slots": {"pipeline": "chipseq",
                                          "protocol": "chipseq"}},
                               {"protocol": "atacseq"})
    r.check("chipseq's optional design is not reported as missing",
            not any("needs a design" in n for n in notes))
    r.check("but the mark column is", any("atac" in n for n in notes))

    # ------------------------------------------------------------------ #
    r.section("the rows offered are the ones this run actually has")

    rows = dict(modify.rows_for(PROPOSAL, "chipseq-0728"))
    r.check("name comes first",
            modify.rows_for(PROPOSAL, "x")[0][0] == "name")
    r.check("a germline run is not offered a pairs file",
            "pairs" not in dict(modify.rows_for(
                {"slots": {"pipeline": "dnaseq", "protocol": "germline_snv"}})))
    r.check("a somatic one is",
            "pairs" in dict(modify.rows_for(
                {"slots": {"pipeline": "dnaseq",
                           "protocol": "somatic_fastpass"}})))
    r.check("a pipeline with no -t is not asked for a protocol",
            "protocol" not in dict(modify.rows_for(
                {"slots": {"pipeline": "rnaseq_light"}})))
    r.equal("current values come from the proposal", rows["steps"], "1-5")

    # ------------------------------------------------------------------ #
    r.section("/reject is terminal, and leaves no pending decision behind")

    with tempfile.TemporaryDirectory() as tmp:
        registry = runs.Registry(tmp)
        registry.hold("chipseq-0728", "chat-1", PROPOSAL, tmp)
        r.equal("held to begin with", len(registry.held()), 1)

        registry.abandon("chipseq-0728", "wrong samples")
        r.equal("gone from held()", len(registry.held()), 0)
        r.equal("gone from /list", len(registry.live()), 0)
        record = registry.get("chipseq-0728")
        r.equal("but still in history", record["status"], runs.ABANDONED)
        r.equal("with the reason kept", record["abandoned_because"],
                "wrong samples")
        r.check("and nothing submitted", record["job_list"] is None)

        # A submitted run is not abandonable: its name is the handle for jobs
        # that really are on the scheduler.
        registry.mark_submitted("live-0728", os.path.join(tmp, "jl"))
        registry.abandon("live-0728", "no")
        r.equal("a submitted run is left alone",
                registry.get("live-0728")["status"], runs.SUBMITTED)

    # ------------------------------------------------------------------ #
    r.section("a rename is a registry write, and leaves no ghost")

    with tempfile.TemporaryDirectory() as tmp:
        registry = runs.Registry(tmp)
        registry.hold("chipseq-chipseq-0728", "chat-1", PROPOSAL, tmp)
        # Reach the gate twice on the same run, which is what a modify cycle
        # does -- so there are two records under the old name to rewrite.
        registry.hold("chipseq-chipseq-0728", "chat-1", PROPOSAL, tmp)

        settled = registry.rename("chipseq-chipseq-0728", "h3k27ac-rep1")
        r.equal("renamed", settled, "h3k27ac-rep1")
        r.check("findable under the new name",
                registry.get("h3k27ac-rep1") is not None)
        # The ghost. get() takes the LAST record for a name, so rewriting only
        # the current one would leave the run reachable under a name /list no
        # longer shows -- approvable, and invisible.
        r.equal("and not under the old one",
                registry.get("chipseq-chipseq-0728"), None)
        r.equal("still held", registry.get("h3k27ac-rep1")["status"], runs.HELD)
        r.equal("and still the thread's held run",
                registry.held_for_thread("chat-1")["name"], "h3k27ac-rep1")

        # A collision gets the -2 suffix rather than shadowing.
        registry.hold("taken", "chat-2", PROPOSAL, tmp)
        registry.hold("other", "chat-3", PROPOSAL, tmp)
        r.equal("a taken name is suffixed",
                registry.rename("other", "taken"), "taken-2")

        registry.mark_submitted("gone-live", os.path.join(tmp, "jl"))
        r.equal("a submitted run refuses to be renamed",
                registry.rename("gone-live", "anything"), None)

    # ------------------------------------------------------------------ #
    # The override ini is named after the run, and override.py's docstring
    # promises that is what stops "two runs tuned differently" sharing one file.
    # Nothing enforced it when the NAME moved, in either of the two ways a name
    # can move.
    r.section("a run's override ini survives its name moving")

    tuned = {
        "command": "bash cmd.sh",
        "generated": ("genpipes chipseq -t chipseq -s 1-5 -c chipseq.base.ini "
                      "rorqual.ini /work/pouletrun.override.ini -g cmd.sh"),
        "slots": {"pipeline": "chipseq", "protocol": "chipseq", "steps": "1-5",
                  "design": None, "pairs": None, "readset": None,
                  "inis": ["chipseq.base.ini", "rorqual.ini",
                           "pouletrun.override.ini"], "output_dir": None},
    }
    r.equal("the ini on the -c stack is found by reading, not deriving",
            modify.stacked_override(tuned), "/work/pouletrun.override.ini")
    r.equal("a command with no override of ours reports none",
            modify.stacked_override(PROPOSAL), "")
    # A GenPipes ini that merely happens to be last is not one of ours.
    r.equal("and a plain ini last in the stack is not mistaken for one",
            modify.stacked_override(
                {"generated": "genpipes chipseq -c a.ini rorqual.ini"}), "")

    # The fork. Its -c starts as a verbatim copy of the parent's, so appending
    # its own ini would leave BOTH -- and the parent's file would go on tuning a
    # run that is supposed to have been given its own copy.
    forked = modify._deltas(tuned, {"resources": "/work/chickenrun.override.ini"})
    r.check("a fork is told to REPLACE its parent's ini, not append beside it",
            any("replace /work/pouletrun.override.ini" in d for d in forked),
            forked)
    r.check("and told the old one must not appear at all",
            any("must not appear at all" in d for d in forked), forked)
    r.check("the new one still has to go last, or the cluster ini wins",
            any("very END of the -c stack" in d for d in forked), forked)

    # Re-tuning the same run is still an append: there is nothing to displace.
    same = modify._deltas(tuned, {"resources": "/work/pouletrun.override.ini"})
    r.check("re-tuning one's own ini stays an append",
            any(d.startswith("append ") for d in same), same)

    with tempfile.TemporaryDirectory() as tmp:
        # THE RENAME. The file stays where it was written and path_for reads the
        # command to find it. Moving it instead would mean editing a command the
        # model produced -- locally, which modify.py exists to never do, or by
        # regenerating, which would put a model call behind the one change at
        # the gate that costs nothing.
        live = os.path.join(tmp, "pouletrun.override.ini")
        override.write(live, {"gatk_sam_to_fastq": {"cluster_walltime":
                                                    "35:00:00"}}, run="pouletrun")
        renamed = dict(tuned)
        renamed["generated"] = tuned["generated"].replace(
            "/work/pouletrun.override.ini", live)
        r.equal("after a rename, path_for still finds the live file",
                override.path_for("chickenrun", tmp, renamed), live)
        r.truthy("so the tuning is still visible at the gate",
                 override.summary(override.read(
                     override.path_for("chickenrun", tmp, renamed))))
        # Without the proposal it would go looking under the new name, find
        # nothing, and the next /modify would write a SECOND ini beside the live
        # one -- two files, one of them in force, neither obviously the real one.
        r.check("the name alone would have pointed at nothing",
                not os.path.exists(override.path_for("chickenrun", tmp)))

        # A run that has never been tuned still gets a path to create.
        r.equal("an untuned run derives its path from its name",
                override.path_for("freshrun", tmp, PROPOSAL),
                os.path.join(tmp, "freshrun.override.ini"))

        # THE FORK gets its own copy, so re-tuning one cannot re-tune the other.
        mine = os.path.join(tmp, "chickenrun.override.ini")
        r.truthy("a fork copies its parent's ini", override.copy(live, mine))
        r.equal("with the same contents",
                override.read(mine), override.read(live))
        override.write(mine, {"gatk_sam_to_fastq": {"cluster_walltime":
                                                    "70:00:00"}}, run="chickenrun")
        r.equal("and re-tuning the fork leaves the parent alone",
                override.read(live)["gatk_sam_to_fastq"]["cluster_walltime"],
                "35:00:00")
        r.check("copying from a run with no ini is a no-op",
                not override.copy(os.path.join(tmp, "nothing.ini"), mine))

        # The deletion message. write() returns '' both for "deleted it" and for
        # "there was never one", and the screen used to announce a removal on
        # the strength of that empty string alone -- claiming a file had gone
        # when none had ever existed.
        r.check("removed() reports a file that actually went",
                override.removed(mine))
        r.check("and refuses to claim one that was never there",
                not override.removed(os.path.join(tmp, "never.override.ini")))

    # ------------------------------------------------------------------ #
    # THE REGRESSION FOR "EDITING A FORK EDITED ITS PARENT".
    #
    # The rule the section above documents -- path_for reads the -c line to
    # find the live file, so a rename does not orphan it -- is right for a run
    # being EDITED and exactly wrong for one being COPIED. A fork quotes its
    # parent's command verbatim, so stacked_override() finds the PARENT's ini
    # on that -c line; tuning a step in the fork then wrote into the parent's
    # file, and a run somebody had gone out of their way not to touch silently
    # acquired a new walltime.
    #
    # The two runs a fork exists to keep separate were sharing one file again,
    # which is the precise thing naming the file after the run is supposed to
    # prevent. And it was silent: nothing failed, nothing was reported, and the
    # parent's own /view then showed the new number as though it had always
    # been there.
    #
    # What is asserted below is the parent's state ACROSS the child's edit, on
    # all four axes it could have moved on -- its ini's path and bytes, its -c
    # stack, its generated command and script, and its registry identity.
    r.section("tuning a fork never touches the run it was forked from")

    with tempfile.TemporaryDirectory() as forkdir:
        parent_ini = os.path.join(forkdir, "pouletrun.override.ini")
        override.write(parent_ini,
                       {"gatk_sam_to_fastq": {"cluster_walltime": "35:00:00"}},
                       run="pouletrun")

        parent = {
            "command": "bash pouletrun.sh",
            "generated": (f"genpipes chipseq -t chipseq -s 1-5 "
                          f"-c chipseq.base.ini rorqual.ini {parent_ini} "
                          f"-g pouletrun.sh"),
            "slots": {"pipeline": "chipseq", "protocol": "chipseq",
                      "steps": "1-5", "design": None, "pairs": None,
                      "readset": "readset.tsv",
                      "inis": ["chipseq.base.ini", "rorqual.ini", parent_ini],
                      "output_dir": None},
        }

        registry = runs.Registry(tempfile.mkdtemp())
        registry.hold("pouletrun", "chat-1", parent, forkdir)

        # Everything about the parent, recorded BEFORE the child is touched.
        before = {
            "ini_path": override.path_for("pouletrun", forkdir, parent),
            "ini_bytes": open(parent_ini, "rb").read(),
            "stack": list(modify.config_stack(parent)),
            "generated": parent["generated"],
            "script": parent["command"],
            "record": dict(registry.get("pouletrun")),
        }

        # THE CHILD'S EDIT, through the same calls cli._fill_resources makes:
        # path_for to decide where, read/merge to build it, write to land it.
        # `fresh` is what the fork path passes and a rewrite does not.
        child_ini = override.path_for("pouletrun-2", forkdir, parent, fresh=True)
        r.equal("a fork's ini is named after the fork, not the parent",
                os.path.basename(child_ini), "pouletrun-2.override.ini")
        r.check("and it is a different file from the parent's",
                os.path.abspath(child_ini) != os.path.abspath(parent_ini))

        override.write(child_ini,
                       override.merge(override.read(child_ini),
                                      "gatk_sam_to_fastq",
                                      {"cluster_walltime": "71:00:00"}),
                       run="pouletrun-2")

        # THE PARENT, AFTER. Four axes, none of which may have moved.
        r.equal("the parent's ini is still the same file",
                override.path_for("pouletrun", forkdir, parent),
                before["ini_path"])
        r.equal("...and its contents are byte-identical",
                open(parent_ini, "rb").read(), before["ini_bytes"])
        r.contains("...still holding the walltime it was written with",
                   before["ini_bytes"].decode(), "35:00:00")
        r.check("...and not the one the fork was tuned to",
                "71:00:00" not in open(parent_ini).read())

        r.equal("the parent's -c stack is unchanged",
                list(modify.config_stack(parent)), before["stack"])
        r.check("...and does not carry the fork's ini",
                child_ini not in modify.config_stack(parent))

        r.equal("the parent's generated command is unchanged",
                parent["generated"], before["generated"])
        r.equal("...and the script it runs is unchanged",
                parent["command"], before["script"])

        after = registry.get("pouletrun")
        r.equal("the parent's registry record is unchanged", dict(after),
                before["record"])
        r.equal("...still held", after["status"], runs.HELD)
        r.equal("...still on its own conversation", after["thread_id"], "chat-1")
        r.equal("...and still the only record there is", len(registry.load()), 1)

        # The fork's own file really was written -- without this every
        # assertion above would pass on a child edit that never happened.
        r.contains("the fork's ini holds the fork's tuning",
                   open(child_ini).read(), "71:00:00")

        # THE NEGATIVE CONTROL, which is what makes this a regression test
        # rather than a description. Without `fresh`, path_for resolves to the
        # parent's file -- correct for a rewrite, and the exact defect when the
        # caller is a fork. If this ever stops being true, the assertions above
        # have stopped being able to fail.
        r.equal("without fresh, path_for still resolves to the parent's file",
                os.path.abspath(override.path_for("pouletrun-2", forkdir, parent)),
                os.path.abspath(parent_ini))

        # And the model is told to displace the inherited ini rather than sit
        # beside it, so the parent's file cannot go on tuning the fork either.
        deltas = modify._deltas(parent, {"resources": child_ini})
        r.check("the fork is told to replace the ini it inherited",
                any(f"replace {parent_ini}" in d for d in deltas), deltas)

    # ------------------------------------------------------------------ #
    # The rule stays -- the name is typed as a CLI argument, built into
    # `{name}.override.ini`, and embedded in a fork's thread id, so a space or a
    # slash breaks a real thing. What changed is that a refusal now hands back
    # the corrected name instead of restating the rule and walking away.
    r.section("an illegal run name is corrected, not just refused")

    r.check("a plain name is fine", modify.valid_name("pouletrun"))
    r.check("digits may lead", modify.valid_name("2run"))
    r.check("dots, dashes and underscores are allowed",
            modify.valid_name("run-2.a_b"))
    r.check("a slash is not", not modify.valid_name("test_/modify"))
    r.check("nor is a space", not modify.valid_name("my run"))
    r.check("nor is a leading dot", not modify.valid_name(".hidden"))

    # The real answer somebody typed at this prompt. It would have tried to
    # write `test_/modify_steps.override.ini` -- a directory that is not there.
    r.equal("the typed name comes back recognisable",
            modify.sanitize("test_/modify_steps"), "test_modify_steps")
    r.equal("a space becomes an underscore", modify.sanitize("my run 2"),
            "my_run_2")
    r.equal("runs of substituted characters collapse",
            modify.sanitize("a///b"), "a_b")
    r.equal("leading punctuation is dropped, not underscored",
            modify.sanitize("--help"), "help")
    r.equal("and a name with nothing legal in it gives nothing back",
            modify.sanitize("..."), "")

    bad = modify.check("name", "test_/modify_steps", None)
    r.check("the verdict refuses", not bad)
    r.contains("and says why the rule exists, not just what it is",
               bad.message, "used as a filename")
    r.equal("offering the corrected name as the thing to pick",
            [o.value for o in bad.options], ["test_modify_steps"])

    hopeless = modify.check("name", "...", None)
    r.equal("a name with no correction offers none", hopeless.options, [])
    r.check("a legal name passes with nothing to offer",
            modify.check("name", "pouletrun", None))

    # ------------------------------------------------------------------ #
    # The commonest thing anybody wants from a finished run is to run it again
    # with one thing different -- a second readset, another database, a
    # re-sequenced sample. /modify used to refuse outright on anything not
    # held, so the answer was to retype the command by hand.
    r.section("a finished run can be copied, never rewritten")

    base = ("genpipes dnaseq -t somatic_fastpass -s 1-5 -r readset_a.tsv "
            "-c dnaseq.base.ini rorqual.ini -g cmd.sh")
    done = {"command": "bash cmd.sh", "generated": base,
            "slots": {"pipeline": "dnaseq", "protocol": "somatic_fastpass",
                      "steps": "1-5", "readset": "readset_a.tsv", "design": None,
                      "pairs": None, "inis": ["dnaseq.base.ini", "rorqual.ini"],
                      "output_dir": None}}

    prose = modify.fork_prose(done, "use readset_b.tsv instead")
    r.contains("the base command travels with the request", prose, base)
    r.contains("along with what should differ", prose, "readset_b.tsv")
    r.contains("and nothing else may move", prose, "nothing else changed")
    r.contains("and it must stop at the gate", prose, "Stop at the gate")

    # A thread that has never seen the original will invent the flags nobody
    # mentioned, which is the whole reason the command is quoted rather than
    # described.
    r.check("every flag of the original is carried, not summarised",
            all(flag in prose for flag in ("-t", "-s", "-r", "-c", "-g")))

    # Nothing to copy from is a real answer and has to be said, not guessed at.
    scanned = {"command": "", "slots": {"pipeline": "dnaseq",
                                        "protocol": "somatic_fastpass"}}
    r.equal("a run found on disk has no command to fork",
            modify.fork_prose(scanned, "use readset_b.tsv"), "")
    r.equal("and neither does an empty request",
            modify.fork_prose(done, "   "), "")

    # The row-driven fork says the same thing in deltas rather than prose.
    picked = modify.fork_sentence(done, {"readset": "readset_b.tsv"})
    r.contains("the guided fork also quotes the base", picked, base)
    r.contains("and names both sides of the change", picked,
               "change -r from readset_a.tsv to readset_b.tsv")

    # ------------------------------------------------------------------ #
    r.section("rows are filled in dependency order, not tick order")

    # The reordering is the point -- the legal step range comes from the
    # protocol's --help, so asking "which steps?" first asks about a protocol
    # that is at that moment still being changed.
    r.equal("the pipeline settles before anything read against it",
            modify.fill_order(["steps", "name", "protocol", "pipeline"]),
            ["pipeline", "protocol", "steps", "name"])
    r.equal("name goes last: it is the row that changes no flag",
            modify.fill_order(["name", "output"]), ["output", "name"])
    r.equal("an order that already agrees is left alone",
            modify.fill_order(["pipeline", "protocol"]),
            ["pipeline", "protocol"])

    # ------------------------------------------------------------------ #
    r.section("-c is a stack, so config is edited by toggling")

    # Every other row answers "what should this BE" and one Enter replaces one
    # value. `-c` is a list whose ORDER decides the run's parameters, and the
    # edit people actually want is "the same stack, plus one" or "minus one".
    STACKED = {"slots": dict(PROPOSAL["slots"],
                             inis=["$GENPIPES_INIS/dnaseq/dnaseq.base.ini",
                                   "$GENPIPES_INIS/common_ini/rorqual.ini",
                                   "$GENPIPES_INIS/dnaseq/cit.ini"],
                             pipeline="dnaseq", protocol="somatic_fastpass")}

    r.equal("the stack reads back in order",
            [os.path.basename(x) for x in modify.config_stack(STACKED)],
            ["dnaseq.base.ini", "rorqual.ini", "cit.ini"])

    # Matched by BASENAME. The stack holds full $GENPIPES_INIS paths while the
    # options list knows the feature ini as a bare filename, so comparing the
    # strings would offer to add a second copy of an ini already there.
    dropped = modify.toggle_config(STACKED, {}, "cit.ini")
    r.equal("toggling a bare name takes the full path off the stack",
            [os.path.basename(x) for x in dropped],
            ["dnaseq.base.ini", "rorqual.ini"])

    added = modify.toggle_config(STACKED, {"config": dropped},
                                 "dnaseq.cancer.ini")
    r.equal("and one that is not on it goes on the end",
            [os.path.basename(x) for x in added],
            ["dnaseq.base.ini", "rorqual.ini", "dnaseq.cancer.ini"])

    r.equal("the delta is add-and-drop, never the whole stack",
            modify.config_delta(STACKED, {"config": added}),
            (["dnaseq.cancer.ini"], ["$GENPIPES_INIS/dnaseq/cit.ini"]))

    # The instruction the model gets. It must be a DIFF: told "change -c from
    # a,b,c to a,b,d" a model rewrites the whole -c line, and a rewritten -c
    # line is one whose surviving inis can come back in a different order --
    # which silently changes which ini wins.
    said = modify.sentence(STACKED, {"config": added})
    r.check("the model is told what to add", "add dnaseq.cancer.ini" in said)
    r.check("and what to drop", "drop $GENPIPES_INIS/dnaseq/cit.ini" in said)
    r.check("and to leave the rest where they are",
            "leave every other ini on -c exactly where it is" in said)
    r.check("never as a replacement", "change -c from" not in said)

    # Toggled on and straight back off. The change set is then equal to what
    # the run already has, and a regeneration for a diff with no lines in it is
    # a chance for the command to drift with nothing asked for.
    back = modify.toggle_config(STACKED, {"config":
                                          modify.toggle_config(STACKED, {}, "x.ini")},
                                "x.ini")
    r.equal("a stack toggled back to itself is the original",
            back, modify.config_stack(STACKED))
    r.equal("and buys no model call", modify.sentence(STACKED, {"config": back}), "")

    # check() must not treat the list as a string to be stripped, and an empty
    # stack is a strange thing to want rather than a slip -- it is reached one
    # visible removal at a time.
    r.check("a list value passes check", bool(modify.check("config", added, STACKED)))
    r.check("so does an empty one", bool(modify.check("config", [], STACKED)))

    # The options list leads with what is ON the stack: removing something
    # requires seeing it, and suggestions first would bury the real inis.
    offered = modify.options_for("config", STACKED, {"config": ["mine.override.ini"]})
    r.equal("the current stack comes first",
            [o.value for o in offered][:3], STACKED["slots"]["inis"])
    r.check("each says which way enter moves it",
            all("enter" in o.description for o in offered))
    r.check("a local ini is offered to add",
            "mine.override.ini" in [o.value for o in offered])
    r.equal("and nothing is offered twice",
            len({o.value for o in offered}), len(offered))

    # Whether an ini is on the stack decides which way Enter moves it, so it
    # is marked in the LABEL and not only in the description column, which is
    # what a narrow terminal drops first.
    r.check("what is on the stack is ticked in the label",
            all(o.label.startswith("✓") for o in offered[:3]))

    # A REMOVAL HAS TO BE UNDOABLE. cit.ini is no protocol's feature ini and no
    # file in the project directory, so once it is off the stack nothing else
    # in this list would ever offer it again -- and the only way back would be
    # to throw away the whole change set.
    after = modify.options_for("config", STACKED, {}, pending={"config": dropped})
    gone = [o for o in after if o.value.endswith("cit.ini")]
    r.equal("an ini taken off is still listed", len(gone), 1)
    r.contains("and says enter puts it back", gone[0].description, "puts it back")

    # And putting it back is an UNDO, not an addition. Restoring it to the end
    # of the stack would change which ini wins while claiming to restore.
    mixed = modify.toggle_config(STACKED, {"config": dropped}, "mine.override.ini")
    restored = modify.toggle_config(STACKED, {"config": mixed},
                                    "$GENPIPES_INIS/dnaseq/cit.ini")
    r.equal("an ini put back lands where it was, not on the end",
            [os.path.basename(x) for x in restored],
            ["dnaseq.base.ini", "rorqual.ini", "cit.ini", "mine.override.ini"])
    r.equal("and the override ini stays last, where it has to be",
            os.path.basename(restored[-1]), "mine.override.ini")

    # ------------------------------------------------------------------ #
    r.section("/sort hides rows without losing them")

    with tempfile.TemporaryDirectory() as tmp:
        registry = runs.Registry(tmp)
        registry.hold("a", "chat-a", PROPOSAL, tmp)
        registry.hold("b", "chat-b", PROPOSAL, tmp)
        registry.hide("a")
        r.equal("hidden from /list", [x["name"] for x in registry.live()], ["b"])
        r.check("still in /history",
                any(x["name"] == "a" for x in registry.all()))
        registry.hide("a", False)
        r.equal("and reversible", len(registry.live()), 2)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
