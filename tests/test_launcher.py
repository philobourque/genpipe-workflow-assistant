#!/usr/bin/env python
"""The launch contract: what `start_agent.sh` promises a new user.

THE CLAIM THIS SUITE DEFENDS, in the words the README makes it in:

    If GenPipes 6.1.1 already works in your Alliance shell environment, clone
    the repository, go to the directory where you want your GenPipes run to
    live, and launch the repository's start_agent.sh. On first launch the
    assistant sets up its own Python environment and asks for your API key. It
    uses your existing GenPipes/cluster configuration, including your RAP_ID,
    and does not depend on the developer's account.

Every clause of that is a property of one 250-line bash file plus a handful of
README lines, and every one of them can be broken by an edit that looks
harmless. A `cd` added for convenience silently moves every future run into the
git checkout. A path typed while debugging on one account leaves the launcher
working only there. An override documented in the README but never wired up is
an instruction that does nothing.

None of that is caught by any other suite: bash is not imported by anything in
`genpipe/`, and the four suites that drive the app all start it in-process,
below the launcher. So this reads the file as text and asserts on it -- which is
the whole method here, and its limits are worth stating. It cannot prove the
script RUNS correctly on a cluster; `testcases/` does that, by hand, on Rorqual.
What it can prove is that the properties the README sells have not been edited
out, which is the failure mode this project has actually had.

Standard library only, no cluster, no venv: it is in the offline CI job.

Run:  python tests/test_launcher.py
"""
import os
import re
import sys
import unicodedata

from harness import Report

from genpipe import preflight, runs, settings

ROOT = settings.ROOT
LAUNCHER = ROOT / "start_agent.sh"
README = ROOT / "README.md"


# Strings that would mean the launch path had been configured for one person.
# The literal author values are deliberately spelled out: a placeholder in a
# README example is fine, and one of these in the launcher is a bug that only
# shows up on somebody else's account.
PERSONAL = (
    "pbourque",
    "rrg-bourqueg",
    "/lustre",
    "/project/6",
)


def code_lines(text):
    """The launcher with its comments and blank lines removed.

    Every check below that asks "does the script DO x" runs on this rather than
    on the raw file, because this script explains itself at length and its
    comments quote the very failures being guarded against -- including the
    absolute paths and the `cd` that must not appear as code. Grepping the raw
    text would report the explanation as the defect.
    """
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def main():
    r = Report("the launch contract")

    text = LAUNCHER.read_text()
    code = code_lines(text)
    body = "\n".join(code)
    readme = README.read_text()
    # Whitespace-collapsed, for the assertions that are about SENTENCES. The
    # README is hard-wrapped, so a phrase worth asserting on straddles a newline
    # whose position moves whenever a word above it changes; matching the raw
    # text would be testing the line breaks instead of the claim.
    said = " ".join(readme.split())
    said_plain = unicodedata.normalize("NFKD", said.lower())
    said_plain = "".join(c for c in said_plain if not unicodedata.combining(c))

    # ------------------------------------------------------------------ #
    r.section("the working directory is the caller's, and stays that way")
    # THE ONE THAT MATTERS MOST. GenPipes writes its output into the process's
    # working directory and the app resolves a submission's job list against
    # it, so a `cd` anywhere in this script would redirect every future run --
    # silently, and into the git checkout, which is the one place it must never
    # go. The `cd` in the HERE assignment is inside a command substitution and
    # moves a subshell only; that is the single permitted form.
    stray = [l for l in code
             if re.match(r"^\s*(cd|pushd|popd)\s", l)
             and not l.startswith("HERE=")]
    r.check("no cd, pushd or popd in the launcher", not stray, stray)
    r.check("the checkout is found through BASH_SOURCE, not through $PWD",
            "BASH_SOURCE" in body and 'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in body)
    r.check("and the app is launched with PYTHONPATH rather than from the checkout",
            'PYTHONPATH="$HERE" python -m genpipe "$@"' in body, body[-200:])
    r.check("arguments reach the app unchanged", '"$@"' in body)

    # Everything the script reads out of its own checkout is addressed through
    # $HERE. A relative path here would work only when the caller happened to be
    # standing in the checkout -- which is exactly the launch this forbids.
    for needed in ("requirements.txt", ".env"):
        # `echo` lines are messages to the reader, not paths the script opens;
        # they are allowed to name the file however reads best.
        refs = [l for l in code if needed in l and not l.startswith("echo ")]
        r.check(f"{needed} is addressed as $HERE/{needed}",
                refs and all("$HERE/" + needed in l for l in refs), refs)

    # ------------------------------------------------------------------ #
    r.section("nothing in the launch path belongs to one account")
    for token in PERSONAL:
        hits = [l for l in code if token in l]
        r.check(f"the launcher contains no {token!r}", not hits, hits)
    r.check("no absolute home directory of any kind",
            not re.search(r"/home/[a-z]", body), body)
    # $HOME and $USER are how a script refers to whoever is running it; a
    # literal expansion of either is how it refers to whoever wrote it.
    r.check("the venv default is written relative to $HOME",
            '"${GENPIPE_VENV:-$HOME/scratch/biomni-venv}"' in body, body)
    # RAP_ID and JOB_MAIL come from the user's own GenPipes environment. The
    # launcher must not set, default or even mention them: a default allocation
    # here would bill somebody else's jobs to whoever's it was.
    for owned_by_genpipes in ("RAP_ID", "JOB_MAIL"):
        r.check(f"{owned_by_genpipes} is never set by the launcher",
                not re.search(rf"^\s*(export\s+)?{owned_by_genpipes}=", body, re.M),
                body)

    # ------------------------------------------------------------------ #
    r.section("the environment is checked, not assumed")
    # The README says the launcher checks the GenPipes environment. It said so
    # before it did: the only checks were `module` and the Python module, and a
    # profile with no MUGQIC module tree got as far as a paid model call, a
    # generated command and an approval before Lmod said the module was unknown.
    r.check("the module system is checked", "command -v module" in body)
    r.check("and GenPipes itself is checked, by name",
            "module -t avail" in body and "GENPIPES_MODULE" in body, body)
    r.equal("against the same version the app's own code uses",
            re.search(r'GENPIPES_MODULE="([^"]+)"', body).group(1),
            runs.GENPIPES_MODULE)
    r.check("the check can be waived without editing the file",
            "GENPIPE_SKIP_GENPIPES_CHECK" in body)
    r.check("and it is skipped when the cluster is being faked",
            'FAKE' in body and '"$FAKE" = 0' in body)
    # sbatch/squeue/sacct: a warning, deliberately, because generation and the
    # gate still work without them and only the scheduler half goes quiet.
    for cmd in ("sbatch", "squeue", "sacct"):
        r.check(f"{cmd} is checked for", cmd in body, body)
    r.check("but a missing Slurm does not stop the launch",
            "Continuing." in text, text)

    # ------------------------------------------------------------------ #
    r.section("every override the README documents is really wired up")
    # An override named in the README and absent from the script is worse than
    # no override: it sends somebody looking for a bug in their own shell.
    for var, why in (("GENPIPE_PYTHON_MODULE", "which Python module to load"),
                     ("GENPIPE_VENV", "where the venv is built"),
                     ("GENPIPE_SKIP_GENPIPES_CHECK", "launch without GenPipes")):
        r.check(f"{var} is read by the launcher ({why})",
                f"${{{var}" in body, body)
        r.contains(f"and documented in the README", readme, var)
    r.check("the Python module has a default rather than being required",
            '"${GENPIPE_PYTHON_MODULE:-python/3.12.4}"' in body, body)
    r.equal("and the README names the same default",
            "python/3.12.4" in readme, True)
    # THE CONTRADICTION THIS CLOSES. The script used to tell people to edit its
    # own `module load` line while the README promised that nothing in it ever
    # needs editing. One of those had to go, and it was not the promise.
    r.check("no error message tells anyone to edit this file",
            "edit start_agent.sh" not in text
            and "edit the 'module load'" not in text
            and 'edit the "module load"' not in text, text)
    r.contains("and the README still makes the promise", said,
               "nothing inside `start_agent.sh` to edit")

    # ------------------------------------------------------------------ #
    r.section("the venv catches up when requirements.txt changes")
    # The README has always claimed this. What the script actually did was
    # `python -c "import biomni, langgraph"`, which detects a dependency that is
    # ABSENT and nothing else: requirements.txt pins a dozen packages and a
    # moved pin on any of them -- including the langchain-anthropic pin whose
    # own comment records the release where it was wrong -- left the venv stale
    # and silent. The hash is the test now; the import stays as a second one,
    # since a venv can be broken without the file having moved.
    r.check("requirements.txt is hashed", "sha256" in body.lower(), body)
    r.check("the hash is stored in the venv it describes",
            'STAMP="$VENV/' in body, body)
    r.check("a differing hash triggers a reinstall",
            '"$WANT" != "$HAVE"' in body, body)
    r.check("and a broken import still does too",
            "import biomni, langgraph" in body, body)
    # Written only on success: a failed or interrupted install must not leave a
    # stamp claiming the venv matches a file it never installed.
    install = body[body.index("install_requirements()"):]
    install = install[:install.index("}")]
    r.check("the stamp is written only after a successful install",
            "|| return 1" in install and install.index("|| return 1")
            < install.index("STAMP"), install)

    # ------------------------------------------------------------------ #
    r.section("the README's install and launch commands are the ones that work")
    # Launch by path from the analysis directory. The form that was documented
    # for a year -- cd into the checkout, then ./start_agent.sh -- puts a
    # pipeline's output inside the git clone, which is why .gitignore carries
    # twenty entries naming GenPipes' output directories.
    r.contains("the README clones to a fixed path outside any project", readme,
               "git clone https://github.com/philobourque/genpipe-workflow-assistant "
               "~/genpipe-workflow-assistant")
    r.contains("and launches by path from the analysis directory", readme,
               "~/genpipe-workflow-assistant/start_agent.sh")
    r.check("it no longer tells anyone to cd into the checkout and run ./start_agent.sh",
            "cd genpipe-workflow-assistant\n./start_agent.sh" not in readme, )
    r.check("nor to run ./start_agent.sh as the daily command",
            not re.search(r"^\./start_agent\.sh\s*$", readme, re.M), )

    # The four locations a person has to be able to tell apart, each named in
    # the README with the variable (or the absence of one) that moves it.
    for location in ("GENPIPE_AGENT_WORKDIR", "GENPIPE_VENV", "GENPIPE_ENV_FILE"):
        r.contains(f"{location} is documented", readme, location)

    # ------------------------------------------------------------------ #
    r.section("only Rorqual is claimed, and nothing else is")
    # THE SUPPORT CLAIM, kept honest by a test rather than by memory.
    #
    # preflight.CLUSTERS maps six Alliance clusters, because reading the cluster
    # ini off the hostname is how the grammar avoids naming one. That is a
    # portability MECHANISM and it is not a validation claim, and the difference
    # between those two is the whole of this section: the documentation may name
    # another cluster, but it may not describe one as working.
    plain = unicodedata.normalize("NFKD", readme.lower())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    r.contains("the README states where this was validated", said_plain,
               "developed and validated on the digital research alliance of "
               "canada's rorqual cluster using genpipes 6.1.1")
    r.check("and says plainly that nothing else is validated",
            "no other cluster, scheduler or genpipes version has been validated"
            in said_plain
            and "none is currently claimed as supported" in said_plain,
            )
    # The phrases that would re-introduce the claim. "expected to work" is the
    # one this project actually used, about four clusters nobody had run it on.
    for overclaim in ("expected to work", "supported by design",
                      "exercised partially", "should also work",
                      "any alliance cluster", "other alliance clusters are supported"):
        r.check(f"the README never says {overclaim!r}", overclaim not in said_plain, )
    # Each non-Rorqual cluster may be MENTIONED, but never within a sentence
    # that says it works. Checked per line, which is where a claim lives.
    for name in preflight.CLUSTERS:
        if name == "rorqual":
            continue
        bad = [l for l in plain.splitlines()
               if name in l and any(w in l for w in
                                    ("supported", "works", "compatible",
                                     "validated on", "also runs"))
               and "not " not in l and "no other" not in l]
        r.check(f"{name} is never described as working", not bad, bad)

    # ------------------------------------------------------------------ #
    r.section("there is one interface, and it is the terminal")
    # The browser front end drove the agent's UNGATED loop: it called the
    # underlying agent.go() rather than the gated run()/resume(), so a
    # submission never paused for approval. An alternative interface that
    # bypasses the gate is not a feature with a caveat, it is a second door
    # into the one thing this product exists to prevent. It was removed rather
    # than documented, and this is what keeps it removed.
    r.check("no web/ directory in the repository",
            not (ROOT / "web").exists(), )
    r.check("and nothing left in the tree imports one",
            not any("web.server" in f.read_text() or "from web " in f.read_text()
                    for f in (ROOT / "genpipe").glob("*.py")), )
    for gone in ("uvicorn", "fastapi", "web/server.py", "web/index.html",
                 "browser front end"):
        r.check(f"the README no longer documents {gone!r}", gone not in readme, )
        r.check(f"and requirements.txt does not install for it ({gone!r})",
                gone not in (ROOT / "requirements.txt").read_text(), )
    # The claim the product now makes, and the command that has to back it.
    r.contains("the README names /approve as the only way to submit",
               readme, "/approve")

    # ------------------------------------------------------------------ #
    r.section("secrets stay out of the launch path")
    r.check("the launcher contains nothing key-shaped",
            "sk-ant" not in text and "API_KEY=" not in text, )
    # .env lives beside the checkout, never in the directory somebody launched
    # from -- so starting the tool in a shared project directory cannot leave a
    # key in it. settings.py is what decides that; this states the property.
    r.equal("the settings file is anchored to the checkout, not the CWD",
            settings.DEFAULT_PATH.parent, ROOT)
    r.check("and .env is gitignored while the template is not",
            ".env*" in (ROOT / ".gitignore").read_text()
            and "!.env.example" in (ROOT / ".gitignore").read_text())

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
