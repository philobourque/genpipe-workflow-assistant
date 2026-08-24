"""The meter in front of the model: caching, usage, and one narrow repair.

Nothing here talks to a provider. The wrapped model is a stand-in that records
what it was handed and returns what it was told to, which is the only way to
assert on the thing that matters -- what leaves this process -- without a
network call or an invoice.

The repair section carries the most weight. A wrapper that rewrites model
output is one bad condition away from deciding what runs on somebody's cluster,
so most of these checks are about what it declines to touch.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from genpipe import metering
from tests.harness import Report


class Reply:
    """Enough of a chat response to be metered."""

    def __init__(self, content, usage=None):
        self.content = content
        self.usage_metadata = usage


class SystemMessage:
    """Stands in for langchain's SystemMessage.

    The NAME matters: the wrapper finds the system message by class name rather
    than by importing langchain_core, so a stand-in called anything else would
    be testing a code path that does not exist."""

    def __init__(self, content):
        self.content = content


class Human:
    def __init__(self, content):
        self.content = content


class Model:
    """A chat model that records its input and replays scripted replies."""

    def __init__(self, replies=None):
        self.replies = list(replies or [Reply("<solution>ok</solution>")])
        self.seen = []
        self.model = "claude-sonnet-5"

    def invoke(self, messages, *a, **k):
        self.seen.append(messages)
        return self.replies.pop(0) if self.replies else Reply("")

    def bind(self, **kw):
        return "bound"


# The wrapper decides whether to mark a cache breakpoint from the CLASS NAME,
# rather than by importing langchain_anthropic -- so these two subclasses are
# the whole of what distinguishes a provider that caches from one that does not.
class ChatAnthropic(Model):
    pass


class ChatOpenAI(Model):
    pass


def _anthropic(replies=None):
    return ChatAnthropic(replies)


def _other(replies=None):
    return ChatOpenAI(replies)


LONG = "x" * (metering.CACHEABLE + 100)


def main():
    r = Report("the metered model")

    # -------------------------------------------------------------------- #
    r.section("a bare execution payload is wrapped, not rewritten")
    # The measured case: 12.87s of a 48.9s turn spent regenerating a command
    # that was already correct and already complete, because it arrived with no
    # tag around it.
    bare = "#!BASH\ngrep -A5 '^\\[step\\]' /tmp/trace.ini\n"
    fixed = metering.repair(bare)
    r.contains("wrapped in execute", fixed, "<execute>")
    r.contains("and closed", fixed, "</execute>")
    r.contains("the payload survives byte for byte", fixed,
               "grep -A5 '^\\[step\\]' /tmp/trace.ini")
    r.contains("including its interpreter marker", fixed, "#!BASH")
    r.equal("and nothing else is added",
            fixed, "<execute>\n" + bare.strip() + "\n</execute>")

    r.section("every interpreter biomni already knows, and no others")
    for sigil in ("#!BASH", "#!CLI", "#!R", "#!PYTHON", "#!bash"):
        r.contains(f"{sigil} is recovered",
                   metering.repair(f"{sigil}\necho hi"), "<execute>")
    r.equal("an invented marker is not",
            metering.repair("#!PERL\nprint 1"), "#!PERL\nprint 1")

    # -------------------------------------------------------------------- #
    r.section("anything already tagged is left alone")
    # The model made its choice about the wrapper. Rewriting it here would be
    # this function overruling the decision it exists to preserve.
    for text in ("<execute>\n#!BASH\nls\n</execute>",
                 "<solution>MANNER: it timed out</solution>",
                 "<think>weighing it up</think>",
                 "#!BASH\nls\n</execute>",
                 "Some prose <execute>#!BASH\nls</execute> more prose"):
        r.equal(f"untouched: {text[:28]!r}", metering.repair(text), text)

    r.section("prose that merely contains a command is never executed")
    # THE PROPERTY THIS FUNCTION IS NARROW FOR. A model explaining a command is
    # not a model asking to run one, and the difference is the first character.
    for text in ("You could run:\n#!BASH\nrm -rf /data\n",
                 "To check the trace, use #!BASH grep ...",
                 "Here is what I would do.\n\n#!BASH\nsbatch job.sh",
                 "The log shows the job died.",
                 "```bash\nls\n```"):
        r.equal(f"left as prose: {text[:30]!r}", metering.repair(text), text)

    r.section("and nothing is invented out of nothing")
    for text in ("", None, 42, [], "   "):
        r.equal(f"{text!r} passes through", metering.repair(text), text)

    # -------------------------------------------------------------------- #
    r.section("a complete unwrapped diagnosis gets its <solution> back")
    # The second measured instance of the same protocol failure: 66.67s of a
    # 78.62s turn spent producing a complete, correctly-shaped answer with no
    # tag around it, which biomni discarded and asked for again.
    whole = ("MANNER: the unit ended in state ZZZ after 7 of 7 units.\n"
             "CAUSE: the payload was still advancing when it stopped.\n"
             "EVIDENCE:\n"
             "- widget.log: last line records unit 7\n"
             "- accounting: state ZZZ\n"
             "FIX: raise some_resource in the [step_alpha] section\n"
             "RELAUNCH: 1-9\n"
             "CONFIDENCE: likely")
    fixed = metering.repair(whole)
    r.contains("wrapped", fixed, "<solution>")
    r.contains("and closed", fixed, "</solution>")
    r.equal("with the answer byte for byte inside it",
            fixed, "<solution>\n" + whole + "\n</solution>")
    r.check("nothing was added to the answer itself",
            "MANNER" in fixed and fixed.count("MANNER") == 1)

    r.section("a shorter but still complete answer qualifies")
    r.contains("three headings in order is enough",
               metering.repair("MANNER: a\nCAUSE: b\nCONFIDENCE: unclear"),
               "<solution>")

    r.section("and everything short of that does not")
    for text, why in (
        ("<solution>\nMANNER: a\nCAUSE: b\nCONFIDENCE: x\n</solution>",
         "already wrapped"),
        ("Here is my reading of it.\n\nMANNER: a\nCAUSE: b\nCONFIDENCE: x",
         "prose before the answer"),
        ("MANNER: a\nCAUSE: b\n<execute>\n#!BASH\nls\n</execute>",
         "carries an action as well"),
        ("MANNER: a\nCAUSE: b\n```bash\nls\n```\nCONFIDENCE: x",
         "carries a fenced payload"),
        ("MANNER: a\nCAUSE: b", "only two headings"),
        ("CAUSE: b\nMANNER: a\nCONFIDENCE: x", "headings out of order"),
        ("MANNER: a\nCAUSE: b\nCAUSE: c\nCONFIDENCE: x", "a heading twice"),
        ("The MANNER of death is one question and the CAUSE another; "
         "CONFIDENCE is low.", "prose that merely uses the words"),
        ("I could not work out the cause from these logs.", "a plain refusal"),
    ):
        r.equal(f"left alone: {why}", metering.repair(text), text)

    r.section("the recogniser reads structure, not subject matter")
    # Same skeleton, entirely different domain and vocabulary. If this failed,
    # something in the recogniser would be keyed to a pipeline, a step name or
    # a failure state rather than to the shape of the document.
    for body in (
        "MANNER: q\nCAUSE: w\nCONFIDENCE: certain",
        "MANNER: \u00e9chec\nCAUSE: m\u00e9moire\nCONFIDENCE: likely",
        "MANNER: 1\nCAUSE: 2\nFIX: 3",
        "**MANNER:** bolded\n**CAUSE:** also bolded\n**CONFIDENCE:** unclear",
    ):
        r.contains(f"recognised: {body[:22]!r}",
                   metering.repair(body), "<solution>")

    # -------------------------------------------------------------------- #
    r.section("the repair reaches the response, and is counted")
    llm = _anthropic([Reply("#!BASH\nls -la")])
    meter = metering.Metered(llm)
    out = meter.invoke([SystemMessage(LONG), Human("go")])
    r.contains("the caller sees the tagged form", out.content, "<execute>")
    r.equal("and it was counted once", meter.repairs, 1)

    clean = metering.Metered(_anthropic([Reply("<solution>done</solution>")]))
    clean.invoke([SystemMessage(LONG), Human("go")])
    r.equal("a well-formed reply counts no repair", clean.repairs, 0)

    # -------------------------------------------------------------------- #
    r.section("the system prompt is sent with a cache breakpoint")
    llm = _anthropic()
    meter = metering.Metered(llm)
    meter.invoke([SystemMessage(LONG), Human("first")])
    sent = llm.seen[0]
    r.equal("the system message is now content blocks",
            isinstance(sent[0].content, list), True)
    block = sent[0].content[0]
    r.equal("carrying the same text, unchanged", block["text"], LONG)
    r.equal("marked ephemeral",
            block["cache_control"], {"type": "ephemeral"})
    r.equal("the conversation after it is untouched",
            sent[1].content, "first")
    r.equal("and only the system message is marked",
            sum(1 for m in sent if isinstance(m.content, list)), 1)

    r.section("the breakpoint goes nowhere it would not work")
    small = _anthropic()
    metering.Metered(small).invoke([SystemMessage("short prompt"), Human("go")])
    r.equal("a prompt below the cache floor is sent as it was",
            small.seen[0][0].content, "short prompt")

    openai = _other()
    wrapped = metering.Metered(openai)
    wrapped.invoke([SystemMessage(LONG), Human("go")])
    r.equal("a provider without cache_control is sent as it was",
            openai.seen[0][0].content, LONG)
    r.equal("and says so in its summary", wrapped.summary()["caching"], False)

    already = _anthropic()
    blocks = [{"type": "text", "text": LONG}]
    metering.Metered(already).invoke([SystemMessage(blocks), Human("go")])
    r.equal("a system message somebody else structured is not restructured",
            already.seen[0][0].content, blocks)

    off = _anthropic()
    metering.Metered(off, cache=False).invoke([SystemMessage(LONG), Human("go")])
    r.equal("and caching can be switched off outright",
            off.seen[0][0].content, LONG)

    # -------------------------------------------------------------------- #
    r.section("the provider's usage is read before biomni discards it")
    # Biomni rebuilds every reply as AIMessage(content=...), so these numbers
    # exist only for the duration of the call this wrapper is inside.
    usage = {"input_tokens": 13724, "output_tokens": 502,
             "input_token_details": {"cache_creation": 11394, "cache_read": 0}}
    llm = _anthropic([Reply("<solution>x</solution>", usage)])
    meter = metering.Metered(llm)
    meter.invoke([SystemMessage(LONG), Human("go")])
    row = meter.calls[0]
    r.equal("input tokens", row["input"], 13724)
    r.equal("output tokens", row["output"], 502)
    r.equal("cache written", row["cache_write"], 11394)
    r.equal("cache read", row["cache_read"], 0)
    r.check("and the call was timed", row["seconds"] >= 0)

    r.section("a provider that reports nothing is not filled in with zeros")
    # A zero here would be indistinguishable from a real call that genuinely
    # read nothing from cache, which is the number people would act on.
    quiet = metering.Metered(_anthropic([Reply("<solution>x</solution>")]))
    quiet.invoke([SystemMessage(LONG), Human("go")])
    r.equal("no input count invented", quiet.calls[0]["input"], None)
    r.equal("nor a cache read", quiet.calls[0]["cache_read"], None)
    r.equal("and the summary says None too", quiet.summary()["input"], None)
    r.check("while the duration is still real", quiet.summary()["seconds"] >= 0)

    r.section("a reply that arrives as blocks is repaired just the same")
    # THE BUG THIS CAUGHT. With extended thinking on, a provider returns a LIST
    # of content blocks rather than a string, and a repair that began with
    # isinstance(text, str) declined every one of them -- silently, on exactly
    # the calls it was written for. The text biomni will read is the
    # concatenation of the textual blocks; that is what must be repaired.
    body = "MANNER: a\nCAUSE: b\nCONFIDENCE: unclear"
    blocks = [{"type": "thinking", "thinking": "private working"},
              {"type": "text", "text": body}]
    llm = _anthropic([Reply(blocks, None)])
    meter = metering.Metered(llm)
    out = meter.invoke([SystemMessage(LONG), Human("go")])
    r.equal("the repair fired", meter.repairs, 1)
    r.equal("and the content is now the wrapped text",
            out.content, "<solution>\n" + body + "\n</solution>")
    r.check("the reasoning block is not carried into it",
            "private working" not in str(out.content))
    r.equal("while the record still describes what arrived",
            meter.calls[0]["shape"]["blocks"], {"thinking": 1, "text": 1})

    r.section("blocks that need no repair are left as blocks")
    keep = [{"type": "thinking", "thinking": "w"},
            {"type": "text", "text": "<solution>MANNER: a</solution>"}]
    llm = _anthropic([Reply(list(keep), None)])
    meter = metering.Metered(llm)
    out = meter.invoke([SystemMessage(LONG), Human("go")])
    r.equal("nothing repaired", meter.repairs, 0)
    r.equal("and the reply is untouched", out.content, keep)

    r.section("a reply with no visible text at all is not repaired")
    # A call that spends its whole token budget inside a reasoning block
    # returns no text. There is nothing to wrap, and inventing something would
    # be this wrapper answering on the model's behalf.
    llm = _anthropic([Reply([{"type": "thinking", "thinking": "all of it"}], None)])
    meter = metering.Metered(llm)
    meter.invoke([SystemMessage(LONG), Human("go")])
    r.equal("no repair attempted", meter.repairs, 0)
    r.equal("and the emptiness is visible in the record",
            meter.calls[0]["shape"]["text"], 0)

    r.section("the reply's structure is recorded, and never its text")
    llm = _anthropic([Reply("<solution>a secret working</solution>",
                            {"input_tokens": 5, "output_tokens": 6,
                             "input_token_details": {}})])
    meter = metering.Metered(llm)
    meter.invoke([SystemMessage(LONG), Human("go")])
    shape = meter.calls[0]["shape"]
    r.equal("a plain string reply counts as one text block",
            shape["blocks"], {"text": 1})
    r.equal("with its length", shape["text"], len("<solution>a secret working</solution>"))
    r.check("and no text is kept anywhere in the row",
            "secret" not in repr(meter.calls[0]), meter.calls[0])

    r.section("block types are counted without their contents")
    blocks = [{"type": "thinking", "thinking": "private working here"},
              {"type": "text", "text": "the answer"},
              {"type": "text", "text": "and more"}]
    llm = _anthropic([Reply(blocks, None)])
    meter = metering.Metered(llm)
    meter.invoke([SystemMessage(LONG), Human("go")])
    shape = meter.calls[0]["shape"]
    r.equal("every type counted", shape["blocks"], {"thinking": 1, "text": 2})
    r.equal("visible characters measured", shape["chars"]["text"],
            len("the answer") + len("and more"))
    r.equal("a reasoning block contributes no characters",
            shape["chars"]["thinking"], 0)
    r.check("and its contents are nowhere in the record",
            "private working" not in repr(meter.calls[0]))

    r.section("the stop reason is carried through")
    llm = _anthropic([Reply("x", None)])
    llm.replies[0].response_metadata = {"stop_reason": "max_tokens"}
    meter = metering.Metered(llm)
    meter.invoke([SystemMessage(LONG), Human("go")])
    r.equal("as reported", meter.calls[0]["stop"], "max_tokens")

    r.section("one row per call, and rows are never merged")
    # The accounting question this answers: a total that is twice what it
    # should be is either one call counted twice or two calls summed. raw()
    # exists so that is answerable rather than arguable.
    llm = _anthropic([Reply("a", {"input_tokens": 7, "output_tokens": 1,
                                  "input_token_details": {}}),
                      Reply("b", {"input_tokens": 7, "output_tokens": 1,
                                  "input_token_details": {}})])
    meter = metering.Metered(llm)
    meter.invoke([SystemMessage(LONG), Human("1")])
    r.equal("one call, one row", len(meter.raw()), 1)
    meter.invoke([SystemMessage(LONG), Human("2")])
    r.equal("two calls, two rows", len(meter.raw()), 2)
    r.equal("each row holds its own call's numbers",
            [row["input"] for row in meter.raw()], [7, 7])
    r.equal("and the total is their sum, not a running field",
            meter.summary()["input"], 14)

    r.section("totals add up across a turn")
    llm = _anthropic([
        Reply("a", {"input_tokens": 100, "output_tokens": 10,
                    "input_token_details": {"cache_creation": 90, "cache_read": 0}}),
        Reply("b", {"input_tokens": 120, "output_tokens": 20,
                    "input_token_details": {"cache_creation": 0, "cache_read": 90}}),
    ])
    meter = metering.Metered(llm)
    meter.invoke([SystemMessage(LONG), Human("one")])
    meter.invoke([SystemMessage(LONG), Human("two")])
    s = meter.summary()
    r.equal("two calls", s["calls"], 2)
    r.equal("input summed", s["input"], 220)
    r.equal("output summed", s["output"], 30)
    r.equal("cache written", s["cache_write"], 90)
    r.equal("cache read", s["cache_read"], 90)
    r.equal("caching was on", s["caching"], True)

    # -------------------------------------------------------------------- #
    r.section("it stays a drop-in for the model it wraps")
    llm = _anthropic()
    meter = metering.Metered(llm)
    r.equal("attributes delegate", meter.model, "claude-sonnet-5")
    r.equal("so do methods", meter.bind(temperature=0), "bound")
    r.equal("a missing one still raises",
            isinstance(getattr(meter, "nope", None), type(None)), True)

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
