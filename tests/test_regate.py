#!/usr/bin/env python
"""The gate survives a conversation, and a superseded proposal never comes back.

Drives the REAL graph -- the actual GenpipeA1, the actual LangGraph interrupt,
the actual SqliteSaver -- with a scripted model, so what is proved here is the
mechanism rather than a description of it.

The two properties, both learned from records sitting in a live registry:

  1. A QUESTION AT THE GATE IS NOT AN ABANDONMENT. Resuming the graph with
     feedback consumes the interrupt, so a model that answers in prose without
     re-proposing used to leave a run that /list called "waiting for approval"
     and /approve called "not waiting at the gate any more". The decision is
     restored for real now -- a fresh interrupt, no model call -- and this
     checks that /approve can actually spend it.

  2. A PROPOSAL THE CONVERSATION HAS MOVED PAST IS NOT RESTORED. ampliconseq-
     0804-2 was rejected with "put it in a new output directory", the model
     regenerated into one, the turn ended, and the old proposal was re-held
     pointing at the directory its owner had just refused. That must be
     impossible, and refusing to restore is the honest outcome -- inventing a
     gate for a stale command is the failure, not the absence of one.

Run:  GENPIPE_FAKE=1 python tests/test_regate.py
"""
import os
import pathlib
import shutil
import sys
import tempfile

from harness import (Report, ScriptedLLM, execute_block, solution,
                     submission_environment)

from genpipe import cli
from genpipe import display
from genpipe import fakecluster
from genpipe import gate
from genpipe import modify
from genpipe import runs as runs_store
from genpipe.cli import build_agent

GEN = ("module load mugqic/genpipes/6.1.1 && genpipes rnaseq -t stringtie "
       "-s 1-5 -c rnaseq.base.ini common_ini/rorqual.ini -r readset.tsv "
       "-d design.tsv -g cmd.sh")
GEN_ELSEWHERE = GEN.replace("-g cmd.sh", "-o fresh_out -g fresh_out/cmd.sh")


def fixtures(work):
    """The files the fake genpipes validates against."""
    os.makedirs(os.path.join(work, "common_ini"), exist_ok=True)
    for path in ("rnaseq.base.ini", os.path.join("common_ini", "rorqual.ini")):
        open(os.path.join(work, path), "w").write("[DEFAULT]\ncluster_server=rorqual\n")
    open(os.path.join(work, "readset.tsv"), "w").write("Sample\tReadset\tLibrary\n")
    open(os.path.join(work, "design.tsv"), "w").write("Sample\tContrast\n")


def fixture(work, script):
    """A built agent with a scripted model, in a throwaway directory."""
    agent = build_agent(path=work)
    agent.use_tool_retriever = False
    agent.llm = ScriptedLLM(script)
    return agent


def main():
    # Approves and resubmits after a rework; needs a submission environment.
    submission_environment()
    r = Report("regate: the decision survives a conversation")
    work = tempfile.mkdtemp(prefix="regate-")
    envdir = tempfile.mkdtemp(prefix="regate-env-")
    cli.ENV_PATH = pathlib.Path(envdir) / ".env"
    here = os.getcwd()
    display.VERBOSE = False
    try:
      with fakecluster.session("happy"):
          os.chdir(work)
          fixtures(work)
          # ---------------------------------------------------------------- #
          r.section("a question at the gate does not destroy the decision")

          agent = fixture(work, [
              execute_block(f"#!BASH\n{GEN}"),
              execute_block('propose_submission("cmd.sh")'),
              # The feedback turn: the model ANSWERS and stops. No re-proposal.
              # This is the shape that used to leave a phantom.
              solution("stringtie was chosen because the readset is stranded "
                       "paired-end and you asked for transcript assembly."),
          ])
          status = agent.run("run rnaseq stringtie on readset.tsv", "chat-a")
          name = status.get("name")
          r.truthy("a run reached the gate", name)
          r.equal("and is held", agent.registry.get(name)["status"],
                  runs_store.HELD)
          first = agent.registry.get(name)["revision"]
          r.truthy("carrying a revision", first)

          # A question, delivered exactly as cli._at_the_gate delivers one.
          agent.resume(name, approved=False, feedback="why stringtie?")

          record = agent.registry.get(name)
          r.equal("the run is still held after the question",
                  record["status"], runs_store.HELD)
          r.equal("holding the SAME proposal", record["revision"], first)
          r.check("and the graph is genuinely parked again",
                  agent.gate_interrupt(agent._config("chat-a")) is not None)

          # The invariant, end to end: /list says held, so /approve must work.
          status = agent.resume(name, approved=True)
          r.check("/approve is accepted", status.get("submitted") is not False)
          r.check("and the run left the held state",
                  agent.registry.get(name)["status"] != runs_store.HELD)

          # ---------------------------------------------------------------- #
          r.section("a proposal the conversation moved past is NOT restored")

          agent = fixture(work, [
              execute_block(f"#!BASH\n{GEN}"),
              execute_block('propose_submission("cmd.sh")'),
              # The feedback was a CHANGE. The model regenerates somewhere new
              # -- and then stops, without proposing it. ampliconseq-0804-2.
              execute_block(f"#!BASH\n{GEN_ELSEWHERE}"),
              solution("Regenerated into fresh_out."),
          ])
          status = agent.run("run rnaseq stringtie on readset.tsv", "chat-b")
          name_b = status.get("name")
          original = agent.registry.get(name_b)["proposal"]
          r.contains("the first proposal writes to cmd.sh",
                     original.get("generated"), "-g cmd.sh")

          agent.resume(name_b, approved=False,
                       feedback="put it in a new output directory")

          record = agent.registry.get(name_b)
          r.equal("the run is NOT held on the old proposal",
                  record["status"], runs_store.LAPSED)
          r.check("and no gate was raised for it",
                  agent.gate_interrupt(agent._config("chat-b")) is None)
          # The point of the whole exercise: nothing anywhere is now offering
          # the command that was rejected.
          r.check("the stale command is not approvable",
                  agent.resume(name_b, approved=True).get("submitted") is False)

          # ---------------------------------------------------------------- #
          r.section("an approval authorises one exact proposal")

          # Two proposals differing only in steps must not share an identity,
          # and an identical one must not gain a new one.
          p1 = gate.build_proposal([], 'propose_submission("cmd.sh")')
          p1["generated"] = GEN
          p1["revision"] = gate.revision(p1)
          p2 = dict(p1)
          p2["generated"] = GEN.replace("-s 1-5", "-s 3-6")
          p2["revision"] = gate.revision(p2)
          p3 = dict(p1)
          p3["revision"] = gate.revision(p3)

          r.check("changing steps changes the revision",
                  p1["revision"] != p2["revision"])
          r.equal("an identical proposal keeps its revision",
                  p1["revision"], p3["revision"])
          for flag, old, new in (("readset", "-r readset.tsv", "-r other.tsv"),
                                 ("design", "-d design.tsv", "-d other.tsv"),
                                 ("config", "rnaseq.base.ini", "rnaseq.other.ini"),
                                 ("output", "-g cmd.sh", "-o out -g cmd.sh")):
              moved = dict(p1)
              moved["generated"] = GEN.replace(old, new)
              moved["revision"] = gate.revision(moved)
              r.check(f"changing {flag} changes the revision",
                      moved["revision"] != p1["revision"])

          # And the check that spends it. A record whose revision has moved on
          # must refuse an interrupt raised for the earlier one.
          agent = fixture(work, [
              execute_block(f"#!BASH\n{GEN}"),
              execute_block('propose_submission("cmd.sh")'),
          ])
          status = agent.run("run rnaseq stringtie on readset.tsv", "chat-c")
          name_c = status.get("name")
          agent.registry.update(name_c, revision="deadbeefcafe")
          out = agent.resume(name_c, approved=True)
          r.equal("a box drawn for another revision cannot be approved",
                  out.get("submitted"), False)
          r.truthy("and says which two disagreed", out.get("revision_mismatch"))
          r.check("nothing was submitted",
                  agent.registry.get(name_c)["status"] != runs_store.SUBMITTED)

          # ---------------------------------------------------------------- #
          r.section("a steps change reaches the script that runs")

          # THE CHAIN THE GATE EXISTS TO GUARANTEE. What /view shows, what the
          # proposal holds, what the generation command says, and what
          # /approve regenerates and launches must all be the same value --
          # not four fields that happen to agree.
          agent = fixture(work, [
              execute_block(f"#!BASH\n{GEN}"),
              execute_block('propose_submission("cmd.sh")'),
              execute_block(f"#!BASH\n{GEN.replace('-s 1-5', '-s 3-6')}"),
              execute_block('propose_submission("cmd.sh")'),
          ])
          status = agent.run("run rnaseq stringtie on readset.tsv", "chat-s")
          name_s = status.get("name")
          r.equal("the gate opens at 1-5",
                  status["proposal"]["slots"]["steps"], "1-5")
          before_rev = agent.registry.get(name_s)["revision"]

          agent.resume(name_s, approved=False, feedback="use steps 3-6 instead")
          record = agent.registry.get(name_s)
          proposal = record["proposal"]
          r.equal("the proposal now says 3-6", proposal["slots"]["steps"], "3-6")
          r.contains("and so does the generation command",
                     proposal["generated"], "-s 3-6")
          r.check("the old value is gone from it",
                  "-s 1-5" not in proposal["generated"])
          r.check("the revision moved", record["revision"] != before_rev)
          r.check("and the old revision is recorded as superseded",
                  before_rev in (record.get("superseded") or []))

          # And the script that actually runs is rebuilt from that same string.
          agent.resume(name_s, approved=True)
          script = os.path.join(work, "cmd.sh")
          r.check("the launched script exists", os.path.exists(script))
          r.contains("and was generated for 3-6", open(script).read(), "3-6")

          # ---------------------------------------------------------------- #
          r.section("the gate reports what moved, not what was asked for")

          # THE PROPERTY. The model is scripted to IGNORE the change: it
          # regenerates the identical command. The gate must not present that
          # as a successful modification.
          agent = fixture(work, [
              execute_block(f"#!BASH\n{GEN}"),
              execute_block('propose_submission("cmd.sh")'),
              execute_block(f"#!BASH\n{GEN}"),          # unchanged. dropped it.
              execute_block('propose_submission("cmd.sh")'),
          ])
          status = agent.run("run rnaseq stringtie on readset.tsv", "chat-i")
          name_i = status.get("name")
          agent._gate_note = {"changed": {"steps": "3-6"}}
          agent.resume(name_i, approved=False, feedback="use steps 3-6 instead")

          verdicts = agent.registry.get(name_i)["changed"]
          r.equal("a dropped change is reported as IGNORED",
                  verdicts.get("steps"), modify.IGNORED)
          r.check("and is NOT green",
                  verdicts.get("steps") != modify.APPLIED)
          r.equal("the value on the record is still the old one",
                  agent.registry.get(name_i)["proposal"]["slots"]["steps"], "1-5")

          # And a change that DOES land reads as applied.
          agent = fixture(work, [
              execute_block(f"#!BASH\n{GEN}"),
              execute_block('propose_submission("cmd.sh")'),
              execute_block(f"#!BASH\n{GEN.replace('-s 1-5', '-s 3-6')}"),
              execute_block('propose_submission("cmd.sh")'),
          ])
          status = agent.run("run rnaseq stringtie on readset.tsv", "chat-j")
          name_j = status.get("name")
          agent._gate_note = {"changed": {"steps": "3-6"}}
          agent.resume(name_j, approved=False, feedback="use steps 3-6 instead")
          r.equal("an applied change is reported as APPLIED",
                  agent.registry.get(name_j)["changed"].get("steps"),
                  modify.APPLIED)

          # A flag nobody asked about, moved by the regeneration, is surfaced.
          agent = fixture(work, [
              execute_block(f"#!BASH\n{GEN}"),
              execute_block('propose_submission("cmd.sh")'),
              execute_block(f"#!BASH\n{GEN.replace('-d design.tsv', '-d other.tsv')}"),
              execute_block('propose_submission("cmd.sh")'),
          ])
          open(os.path.join(work, "other.tsv"), "w").write("Sample\tContrast\n")
          status = agent.run("run rnaseq stringtie on readset.tsv", "chat-k")
          name_k = status.get("name")
          agent._gate_note = {"changed": {"steps": "1-5"}}
          agent.resume(name_k, approved=False, feedback="keep steps 1-5")
          r.equal("an unrequested move is reported as DRIFTED",
                  agent.registry.get(name_k)["changed"].get("design"),
                  modify.DRIFTED)

    finally:
        os.chdir(here)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(envdir, ignore_errors=True)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
