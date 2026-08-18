#!/usr/bin/env python
"""Getting a new person from `git clone` to a working session.

Everything here is about the path somebody walks exactly once and therefore the
path nobody who built the tool ever walks again: no .env, no key, no provider
chosen, and a mistake at any step of it.

Needs the agent stack (cli.py imports biomni) but no API key, no network and no
cluster: every provider call is a stub. The pty-level version of the same
journey -- what the screen actually looks like -- is in test_app.

Run:  python tests/test_onboarding.py
"""
import io
import os
import stat
import sys
import tempfile
from contextlib import redirect_stdout

from harness import Report

from genpipe import cli, display, settings


def drawn(fn, *args, **kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


class FakeLLM:
    """A provider client that answers however the test needs it to."""

    def __init__(self, error=None, model="stub-model"):
        self.error = error
        self.model = model
        self.calls = 0

    def invoke(self, _prompt):
        self.calls += 1
        if self.error:
            raise self.error
        return "hi"


class Rejected(Exception):
    def __init__(self, status_code, text="no"):
        super().__init__(text)
        self.status_code = status_code


class Agent:
    def __init__(self, llm=None):
        self.llm = llm or FakeLLM()


def main():
    r = Report("first run: provider, key, model")
    saved_env = dict(os.environ)
    saved_path = cli.ENV_PATH

    try:
        # -------------------------------------------------------------- #
        r.section("nothing is advertised that is not wired up")
        # Four providers are offered by name, on the key prompt and by /model.
        # Each has to carry the environment variable biomni's get_llm actually
        # reads for it and a default model, or the offer is an invitation to
        # a dead end.
        for prefix, source, env_var, model in cli.KNOWN_PROVIDERS:
            r.check(f"{source}: a prefix, a key variable and a default model",
                    all((prefix, source, env_var, model))
                    and env_var.endswith("_API_KEY"),
                    (prefix, source, env_var, model))
        names = [p[1] for p in cli.KNOWN_PROVIDERS]
        r.check("the /model menu offers exactly those, and no more",
                all(any(n in row for row in cli._provider_names()) for n in names)
                and len(cli._provider_names()) == len(names),
                cli._provider_names())
        r.check("sk-ant- is checked before the bare sk- fallback",
                names.index("Anthropic") < names.index("OpenAI"))
        r.equal("so an Anthropic key is not read as an OpenAI one",
                cli._detect_provider("sk-ant-abc")[0], "Anthropic")
        for key, want in (("sk-proj-abc", "OpenAI"), ("AIzaSyabc", "Gemini"),
                          ("gsk_abc", "Groq")):
            r.equal(f"{key[:6]}… is {want}", cli._detect_provider(key)[0], want)
        r.check("and an unrecognised key is not guessed at",
                cli._detect_provider("hunter2") is None)

        r.section("a placeholder is not a key")
        for placeholder in ("", "sk-ant-...", "sk-...", "AIza...", "gsk_..."):
            r.check(f"{placeholder!r} is refused", cli._looks_like_placeholder(placeholder))
        r.check("a real-shaped key is not",
                not cli._looks_like_placeholder("sk-ant-api03-abcdefgh"))
        # .env.example ships every one of them commented; if one ever stopped
        # matching, a fresh copy of that file would look like a configured key.
        example = (settings.ROOT / ".env.example").read_text()
        for name, value in settings.parse(example).items():
            if name.endswith("_API_KEY"):
                r.check(f".env.example's {name} still reads as a placeholder",
                        cli._looks_like_placeholder(value), value)

        # -------------------------------------------------------------- #
        with tempfile.TemporaryDirectory() as tmp:
            envfile = os.path.join(tmp, ".env")
            cli.ENV_PATH = settings.Path(envfile)

            r.section("the key is asked for once and then never again")
            for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                        "GEMINI_API_KEY", "GROQ_API_KEY"):
                os.environ.pop(var, None)
            asked = {"n": 0}

            def fake_prompt():
                asked["n"] += 1
                os.environ["ANTHROPIC_API_KEY"] = "sk-ant-pretend"
                os.environ["GENPIPE_LLM_SOURCE"] = "Anthropic"
                os.environ["GENPIPE_LLM_MODEL"] = "claude-sonnet-5"

            real_prompt = cli._prompt_for_api_key
            cli._prompt_for_api_key = fake_prompt
            try:
                first, _ = drawn(cli._require_api_key)
                r.check("with no key at all, it asks", first is True and asked["n"] == 1)
                second, _ = drawn(cli._require_api_key)
                r.check("with one configured, it does not",
                        second is False and asked["n"] == 1)
                # The return value is not decoration: it is what lets main()
                # check a BRAND NEW key without paying for a probe on every
                # later launch.
                os.environ["ANTHROPIC_API_KEY"] = "sk-ant-..."
                third, _ = drawn(cli._require_api_key)
                r.check("and a placeholder in .env counts as no key",
                        third is True and asked["n"] == 2)
            finally:
                cli._prompt_for_api_key = real_prompt

            # ---------------------------------------------------------- #
            r.section("a key that does not work is caught at the prompt, not mid-turn")
            # THE DEFECT: /model probed before switching and /key probed before
            # applying, and the first-launch path did neither. A typo was
            # saved, reported as `Saved`, and surfaced a turn later as a
            # provider error from inside the agent loop -- where it reads as a
            # problem with the message rather than with the key.
            os.environ["GENPIPE_LLM_SOURCE"] = "Anthropic"
            os.environ["GENPIPE_LLM_MODEL"] = "claude-sonnet-5"
            bad = Agent(FakeLLM(error=Rejected(401, "authentication_error")))
            ok, out = drawn(cli._confirm_new_key, bad)
            r.check("a rejected key is reported as rejected", ok is False)
            r.contains("naming the provider that rejected it", out, "Anthropic")
            r.contains("and what to do about it", out, "/key")
            r.contains("including the other way out", out, "/model")
            r.check("the session is not ended over it", bad.llm.calls == 1)

            unreachable = Agent(FakeLLM(error=TimeoutError("no route to host")))
            ok, out = drawn(cli._confirm_new_key, unreachable)
            r.check("a rate limit or a dead network is NOT a rejected key",
                    ok is True)
            r.contains("and says the check did not happen", out.lower(), "could not reach")

            good = Agent(FakeLLM())
            ok, out = drawn(cli._confirm_new_key, good)
            r.check("a working key says nothing at all", ok is True and out == "", out)

            # ---------------------------------------------------------- #
            r.section("switching provider never reaches for the wrong key")
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ["ANTHROPIC_API_KEY"] = "sk-ant-pretend"
            before = dict(os.environ)
            agent = Agent()
            _, out = drawn(cli._cmd_model, agent, ["OpenAI"])
            r.contains("a provider with no key of its own is refused", out, "No OpenAI key")
            r.contains("and told where to get one", out, "/key")
            r.check("the model in use is untouched", agent.llm.model == "stub-model")
            r.check("and nothing was written to the settings file",
                    os.environ.get("GENPIPE_LLM_SOURCE") == before.get("GENPIPE_LLM_SOURCE"))

            _, out = drawn(cli._cmd_model, agent, ["Nonesuch"])
            r.contains("an unknown provider is refused", out, "Unknown provider")
            for name in [p[1] for p in cli.KNOWN_PROVIDERS]:
                r.contains(f"and the real ones are listed ({name})", out, name)

            _, out = drawn(cli._cmd_model, agent, [])
            r.contains("/model with no argument states what is in use",
                       out, "Anthropic")
            r.check("and does not print anything key-shaped",
                    "sk-ant" not in out, out)

            # ---------------------------------------------------------- #
            r.section("Ctrl+C inside /key returns to the session")
            # It used to end the process and take the conversation with it --
            # quite possibly with a proposal parked at the gate.
            def cancelled():
                raise SystemExit(1)

            cli._prompt_for_api_key = cancelled
            try:
                agent = Agent()
                result, out = drawn(cli._cmd_key, agent, [])
                r.check("the command returns rather than exiting", result is None)
                r.contains("and says nothing was changed", out, "No key changed")
                r.contains("naming what is safe", out.lower(), "gate")
                r.check("the model in use is exactly as it was",
                        agent.llm.model == "stub-model")
            finally:
                cli._prompt_for_api_key = real_prompt

            # ---------------------------------------------------------- #
            r.section("what is written to disk, and how")
            os.environ["GENPIPE_ENV_FILE"] = envfile
            cli._write_env_var("GENPIPE_LLM_SOURCE", "Anthropic")
            cli._write_env_var("ANTHROPIC_API_KEY", "sk-ant-first")
            cli._write_env_var("GENPIPE_USER", "philo")
            r.check("the file is readable by nobody else",
                    stat.S_IMODE(os.stat(envfile).st_mode) == 0o600,
                    oct(stat.S_IMODE(os.stat(envfile).st_mode)))
            cli._write_env_var("ANTHROPIC_API_KEY", "sk-ant-second")
            body = open(envfile).read()
            r.check("rotating a key replaces it rather than appending",
                    body.count("ANTHROPIC_API_KEY") == 1, body)
            r.check("and leaves everything else in place",
                    "GENPIPE_USER=philo" in body and "sk-ant-first" not in body, body)

            # THE ROUND TRIP. This is the whole reason settings.py exists: what
            # the app writes here is what it reads on the next launch, without
            # a shell in between.
            fresh = {"GENPIPE_ENV_FILE": envfile}
            settings.load(fresh)
            r.equal("and a later launch reads back exactly what was saved",
                    fresh.get("ANTHROPIC_API_KEY"), "sk-ant-second")
            r.equal("including which provider it is for",
                    fresh.get("GENPIPE_LLM_SOURCE"), "Anthropic")

            r.section("a key is never printed in full")
            source = (settings.ROOT / "genpipe" / "cli.py").read_text()
            r.check("only a four-character tail is ever echoed back",
                    "key[-4:]" in source and "print(f\"{key}" not in source)
            r.check("and the masked reader reveals four and stars the rest",
                    "reveal=4" in source
                    and 'ch if len(chars) <= reveal else "*"' in source)
            # /where prints every path this app touches. None of them is a key,
            # and nothing in it reads the environment for one.
            where = (settings.ROOT / "genpipe" / "cli.py").read_text()
            block = where[where.index("def _cmd_where"):where.index("def _cmd_telemetry")]
            r.check("/where names no API key variable",
                    "API_KEY" not in block, block[:200])

    finally:
        cli.ENV_PATH = saved_path
        os.environ.clear()
        os.environ.update(saved_env)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
