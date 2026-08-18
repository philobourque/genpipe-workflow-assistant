#!/usr/bin/env python
"""The settings file, read by the app that writes it.

The defect these are the regression for: the app wrote .env -- key, provider,
model, name -- and never read it. Only start_agent.sh's `source` did, so
`python -m genpipe` asked for an API key it had been given the day before,
and /model reported "no key configured" about a key that was.

Stdlib only. No agent stack, no cluster, no key.

Run:  python tests/test_settings.py
"""
import os
import sys
import tempfile

from harness import Report

from genpipe import settings


def main():
    r = Report("the settings file")

    # ------------------------------------------------------------------ #
    r.section("what a line may look like")
    got = settings.parse(
        "# a comment\n"
        "\n"
        "export ANTHROPIC_API_KEY=sk-ant-abc123\n"
        "GENPIPE_LLM_SOURCE=Anthropic\n"
        'GENPIPE_LLM_MODEL="claude-sonnet-5"\n'
        "GENPIPE_USER='philo'\n"
        "   export GENPIPE_THEME=dark   \n"
        "not a setting at all\n"
    )
    r.check("the exported form _write_env_var uses",
            got.get("ANTHROPIC_API_KEY") == "sk-ant-abc123", got)
    r.check("and the bare form a person types",
            got.get("GENPIPE_LLM_SOURCE") == "Anthropic", got)
    for quoted in ("GENPIPE_LLM_MODEL", "GENPIPE_USER"):
        r.check(f"quotes are stripped from {quoted}",
                got.get(quoted) in ("claude-sonnet-5", "philo"), got)
    r.check("leading and trailing space is not part of the value",
            got.get("GENPIPE_THEME") == "dark", got)
    r.check("a comment defines nothing", "# a comment" not in str(got))
    r.check("and a line that is not an assignment is skipped, not guessed at",
            len(got) == 5, got)

    # A `#` inside a value is not a comment. An API key is an opaque string and
    # guessing at its interior is how half a key gets silently saved.
    hashy = settings.parse("export OPENAI_API_KEY=sk-aa#bb\n")
    r.check("a # inside a value stays in the value",
            hashy.get("OPENAI_API_KEY") == "sk-aa#bb", hashy)

    r.check("unreadable input raises nothing", settings.parse(None) == {})

    # ------------------------------------------------------------------ #
    r.section("loading never overwrites what is already set")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "settings.env")
        with open(path, "w") as fh:
            fh.write("export ANTHROPIC_API_KEY=sk-ant-from-file\n"
                     "export GENPIPE_LLM_MODEL=from-file\n"
                     "export GENPIPE_USER=from-file\n")

        env = {"GENPIPE_ENV_FILE": path,
               "GENPIPE_LLM_MODEL": "already-chosen"}
        added = settings.load(env)
        r.check("a variable already in the environment wins",
                env["GENPIPE_LLM_MODEL"] == "already-chosen", env)
        r.check("and is not reported as having been set",
                "GENPIPE_LLM_MODEL" not in added, added)
        r.check("one that is absent is filled in",
                env["ANTHROPIC_API_KEY"] == "sk-ant-from-file")
        r.check("and is reported", "ANTHROPIC_API_KEY" in added, added)

        # THE PROPERTY THAT MAKES start_agent.sh AND THIS AGREE. The launcher
        # sources .env one line before exec'ing python, so by the time load()
        # runs everything in the file is already in the environment and every
        # single assignment is a no-op. The two paths cannot fight.
        second = settings.load(env)
        r.check("loading twice changes nothing", second == [], second)

        # ---------------------------------------------------------------- #
        r.section("the file may only define things this app understands")
        with open(path, "w") as fh:
            fh.write("export PATH=/evil/bin\n"
                     "export LD_PRELOAD=/evil/lib.so\n"
                     "export PYTHONPATH=/evil\n"
                     "export GENPIPE_THEME=light\n")
        env = {"GENPIPE_ENV_FILE": path}
        added = settings.load(env)
        for dangerous in ("PATH", "LD_PRELOAD", "PYTHONPATH"):
            r.check(f"{dangerous} is not something a .env may set",
                    dangerous not in env, env)
        r.check("but a setting on the list is", env.get("GENPIPE_THEME") == "light")
        r.check("and only that one was reported", added == ["GENPIPE_THEME"], added)

        r.check("every provider key this app knows how to use is allowed",
                {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                 "GROQ_API_KEY"} <= settings.ALLOWED)

    # ------------------------------------------------------------------ #
    r.section("a checkout with no settings file is the ordinary state")
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GENPIPE_ENV_FILE": os.path.join(tmp, "nothing-here.env")}
        r.check("an absent file sets nothing and raises nothing",
                settings.load(env) == [])
        r.check("and leaves the environment alone", list(env) == ["GENPIPE_ENV_FILE"])

    # ------------------------------------------------------------------ #
    r.section("one location, so the reader and the writer cannot disagree")
    r.check("GENPIPE_ENV_FILE moves it",
            str(settings.path({"GENPIPE_ENV_FILE": "/tmp/x.env"})) == "/tmp/x.env")
    r.check("and without it, it is the .env beside the checkout",
            settings.path({}) == settings.DEFAULT_PATH)
    r.check("which is inside the checkout, not the working directory",
            settings.DEFAULT_PATH.name == ".env"
            and settings.DEFAULT_PATH.parent == settings.ROOT)

    # The one that makes the whole file safe to have: cli.ENV_PATH is this,
    # not a second Path() built from cli.__file__. Checked without importing
    # cli, which needs the agent stack.
    source = (settings.ROOT / "genpipe" / "cli.py").read_text()
    r.check("and cli.py writes to the same place it reads from",
            "ENV_PATH = settings.path()" in source)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
