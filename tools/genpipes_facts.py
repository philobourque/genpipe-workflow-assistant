#!/usr/bin/env python
"""Read this GenPipes install's own argument parsers and write down what they say.

WHY THIS EXISTS.

slots.py holds a table of pipelines, protocols and defaults. Every value in it
is correct today, and every value in it was typed by a person -- which means it
is correct until the day GenPipes ships a release that changes one, and wrong
in a way nothing would notice. A table that can silently disagree with the
software it describes is a table that will.

So this generates the same facts from the install, and tests/test_requirements
compares the two. The table stays (it is what the panel and the gate read, and
it must work on a laptop with no GenPipes on it); what changes is that it can
no longer drift without something going red.

WHAT IS AUTHORITATIVE HERE AND WHAT IS NOT -- this is the distinction the whole
exercise is about, and the output keeps the four apart:

  parser_required   arguments argparse itself refuses to run without. `-c` and
                    `-r` on every pipeline. Objective, version-exact, and the
                    only thing here that deserves the word "required" without
                    qualification.
  protocol          the `-t` choices and the literal `default=`. A default is
                    the answer to "which protocol", not the absence of one, so
                    a pipeline that has one must not be interrogated about it.
  optional_defaults what GenPipes does when a flag is omitted. `-s` has no
                    default, which is GenPipes for "every step". `-o` defaults
                    to the working directory. These are FREEDOMS, and the
                    reason to write them down is to stop the agent quietly
                    converting them into requirements.
  conditional       NOT PRODUCED HERE, and the omission is the point. That a
                    somatic protocol needs `-p`, or that ampliconseq's `asva`
                    reads a design, is invisible to argparse -- both flags are
                    optional to the parser and required by a step. That is what
                    slots.py knows and this cannot, and it is why slots.py is
                    not replaced by this file.

Run it on a machine with GenPipes loaded:

    module load genpipes
    python tools/genpipes_facts.py > genpipe/genpipes_facts.json

It imports GenPipes and calls its argparser() classmethods. It runs no
pipeline, submits nothing, and reads no data.
"""
import argparse
import importlib
import inspect
import json
import os
import pkgutil
import sys


def _pipeline_class(module, name):
    """The Pipeline subclass a pipeline package defines, or None.

    The most derived one, because several packages define a base and then
    specialise it, and it is the leaf whose argparser() carries the `-t`.
    """
    from genpipes.core.pipeline import Pipeline

    found = None
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if not issubclass(obj, Pipeline):
            continue
        if not obj.__module__.startswith(f"genpipes.pipelines.{name}"):
            continue
        if found is None or issubclass(obj, found):
            found = obj
    return found


def facts():
    """Every pipeline on this install, as plain data."""
    import genpipes.pipelines as pipelines

    out = {}
    for module in pkgutil.iter_modules(pipelines.__path__):
        name = module.name
        if not module.ispkg or name == "common_ini":
            continue
        loaded = importlib.import_module(f"genpipes.pipelines.{name}")
        cls = _pipeline_class(loaded, name)
        if cls is None:
            continue
        parser = argparse.ArgumentParser(prog=name)
        cls.argparser(parser)

        required, optional, protocol = [], {}, None
        for action in parser._actions:
            if not action.option_strings:
                continue
            flags = "/".join(action.option_strings)
            if action.required:
                required.append(flags)
            if action.dest == "protocol":
                protocol = {"flags": flags,
                            "choices": list(action.choices or []),
                            "default": action.default}
            elif action.dest in ("steps", "output_dir", "job_scheduler",
                                 "design_file", "pairs_file"):
                default = action.default
                # -o defaults to os.getcwd(), which is a fact about where this
                # was run and not about GenPipes. Recorded as what it MEANS, so
                # the manifest is reproducible on another machine.
                if action.dest == "output_dir":
                    default = "<cwd>"
                optional[action.dest] = {"flags": flags, "default": default}
        out[name] = {"class": cls.__name__,
                     "parser_required": sorted(required),
                     "protocol": protocol,
                     "optional_defaults": optional}
    return out


def main():
    try:
        import genpipes                                    # noqa: F401
    except ImportError:
        sys.stderr.write(
            "No GenPipes on this interpreter's path. Load the module first:\n"
            "    module load genpipes\n")
        return 2
    payload = {
        "genpipes_version": _version(),
        "generated_from": "the installed GenPipes' own argparse definitions",
        "pipelines": facts(),
    }
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _version():
    try:
        from genpipes.core.pipeline import Pipeline
        return str(Pipeline.genpipes_version())
    except Exception:
        return os.environ.get("GENPIPES_VERSION", "unknown")


if __name__ == "__main__":
    sys.exit(main())
