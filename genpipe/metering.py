"""A wrapper around the chat model: cache the static prefix, record what it cost.

Three things happen in one place because all three need the same seam -- the
moment between "the graph asked the model" and "the model answered" -- and that
seam is not otherwise reachable. Biomni's generate node builds its own
SystemMessage and parses its own response inside a single function call
(a1.py:1294), so a wrapper around the NODE is too late for two of these and a
patch to biomni is not ours to keep.

What is reachable is `agent.llm`. The compiled graph reads it live on every
step -- cli.py already depends on that to switch models mid-session, and dev
mode replaces it wholesale -- so an object with `.invoke()` in that slot sits
exactly where it needs to be.

  1. CACHING. The system prompt is byte-identical on every call: the same
     ~45 kB of instructions and genpipes.md, resent in full four times in a
     four-call turn, which measured as 83% of one /diagnose turn's entire
     input. Marking it `cache_control: ephemeral` makes the second and later
     calls read it from Anthropic's cache instead. Nothing about the prompt
     changes -- this is the same bytes, transmitted differently.

  2. USAGE. Biomni discards the provider's response object: it builds a fresh
     `AIMessage(content=msg.strip())` from the text (a1.py:1349), so token
     counts, cache hits and the response id never reach the checkpoint. They
     exist for exactly the duration of the call below, which is the only place
     they can be read at all.

  3. SYNTAX REPAIR. Strictly bounded -- see repair().

None of this changes what the model is asked or what it decides. Two of the
three are transport; the third is punctuation.

Standard library plus whatever the wrapped model is. No provider is imported
here: the Anthropic-specific step is guarded by asking the object what it is,
so a session on another provider passes straight through.
"""
import re
import time

from . import diagnosis

# The minimum a cache breakpoint is worth. Anthropic will not cache a prefix
# shorter than about 1024 tokens and returns an error rather than ignoring the
# request, so a short system prompt -- a test double, a stripped-down session --
# must not be marked. Characters, deliberately conservative: this is a floor to
# stay above, not an estimate to be accurate.
CACHEABLE = 8000

# The providers whose API takes cache_control on a content block. Matched on the
# class name rather than by importing langchain_anthropic, which would make this
# module refuse to load in a session that has never had that provider.
CACHING = ("ChatAnthropic", "ChatAnthropicMessages")

# Biomni's execution markers. A response that is nothing but one of these
# followed by a payload is a malformed <execute>, and it is the only shape
# repair() will touch.
_SIGIL = re.compile(r"\A\s*#!\s*(BASH|CLI|R|PYTHON)\b", re.IGNORECASE)

# Any tag at all means the model made its choice about the wrapper and the
# response is not ours to rewrite -- including a `<solution>`, and including a
# closing tag whose opener got lost, which is a different kind of damage that
# biomni already repairs itself.
_TAGGED = re.compile(r"</?\s*(execute|solution|think)\b", re.IGNORECASE)


def repair(text):
    """Put the wrapper back on a response that plainly needed one.

    Two shapes, and nothing else: an execution payload with no <execute> around
    it, and a complete diagnosis with no <solution> around it. Both are the same
    failure -- the model decided, wrote the decision out in full, and left the
    tag off -- and both cost a whole generation when biomni rejects them. Two
    measured turns lost 12.87s and 66.67s that way, and in each the retry
    returned the same content with the tag on it.

    WHY IT IS THIS NARROW. A wrapper that rewrites model output is a short step
    away from deciding what runs on somebody's cluster, so every condition here
    exists to make the case unmistakable rather than merely likely:

      no tag anywhere         a response carrying <execute> has asked for an
                              action and one carrying <solution> needs nothing.
                              Either way the model already said which it meant,
                              and a half-tagged response is damage biomni
                              repairs itself.
      it BEGINS with the      not "contains". Prose quoting a command is
      recognised shape        somebody explaining a command; prose containing
                              headings is somebody writing in shorthand.
                              Running the first, or answering with the second,
                              would be this function inventing the decision it
                              exists to preserve.
      no new vocabulary       the sigils are the ones biomni already documents
                              (#!BASH, #!CLI, #!R, #!PYTHON); the headings are
                              the ones diagnosis.SHAPE already asks for.

    Nothing is extracted, reinterpreted, completed, reformatted or judged. The
    ENTIRE response is wrapped, byte for byte, or nothing happens. If the model
    meant something this cannot express, this cannot express it.
    """
    if not text or not isinstance(text, str):
        return text
    if _TAGGED.search(text):
        return text
    if _SIGIL.match(text):
        return f"<execute>\n{text.strip()}\n</execute>"
    # Structural only -- see diagnosis.complete(). Whether the diagnosis is any
    # good is not asked there and must not become askable there.
    if diagnosis.complete(text):
        return f"<solution>\n{text.strip()}\n</solution>"
    return text


def _visible(response):
    """The text a reply carries, flattened the way biomni flattens it.

    A non-streaming reply is a plain string when the model returns one text
    block and a LIST OF BLOCKS when it returns anything else -- which, with
    extended thinking enabled, is every reply. Biomni concatenates the textual
    parts and ignores the rest (a1.py:1306-1322); this reproduces that exactly,
    because anything else would repair a string biomni never reads.

    Reasoning blocks are skipped rather than read. Their presence is counted in
    _shape(); their contents are not this module's to handle.
    """
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("text", "output_text", "redacted_text"):
            piece = block.get("text") or block.get("content") or ""
            if isinstance(piece, str):
                parts.append(piece)
    return "".join(parts)


def _shape(response):
    """What kind of thing the provider returned, counted, with no content.

    Returns {"blocks": {type: count}, "chars": {type: total}, "text": n} where
    `text` is the character count of the visible content as a whole. A plain
    string reply -- which is what a non-streaming Anthropic call gives when it
    returns one text block -- is reported as one block of type "text".
    """
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return {"blocks": {"text": 1}, "chars": {"text": len(content)},
                "text": len(content)}
    if not isinstance(content, list):
        return {"blocks": {}, "chars": {}, "text": 0}
    blocks, chars = {}, {}
    for item in content:
        if isinstance(item, dict):
            kind = str(item.get("type") or "unknown")
            # Only the field a text block carries its text in is measured, and
            # only its LENGTH. A thinking block's own field is deliberately not
            # read at all.
            size = len(item.get("text") or "") if kind == "text" else 0
        else:
            kind, size = type(item).__name__, 0
        blocks[kind] = blocks.get(kind, 0) + 1
        chars[kind] = chars.get(kind, 0) + size
    return {"blocks": blocks, "chars": chars, "text": chars.get("text", 0)}


def cacheable(llm):
    """Whether marking a cache breakpoint on this model is meaningful."""
    return type(llm).__name__ in CACHING


class Metered:
    """`agent.llm` with a cache breakpoint, usage accounting and tag repair.

    Everything not named here is delegated, so the object stays a drop-in: the
    rest of the app goes on reading `.model`, calling `.bind()`, and passing it
    to _drop_sampling_params without knowing this is in the way.
    """

    def __init__(self, llm, telemetry=None, cache=True):
        self._llm = llm
        self._telemetry = telemetry
        # Asked once, at construction, rather than per call: the wrapped object
        # does not change identity, and a type() check on every invoke is a
        # question already answered.
        self._cache = bool(cache) and cacheable(llm)
        self.repairs = 0
        self.calls = []

    # -- the seam ---------------------------------------------------------- #

    def invoke(self, messages, *args, **kwargs):
        started = time.monotonic()
        response = self._llm.invoke(self._prepared(messages), *args, **kwargs)
        spent = time.monotonic() - started

        # Measured before anything is touched, so the record describes what the
        # provider actually sent rather than what this method left behind.
        shape = _shape(response)

        # REPAIRED AGAINST THE VISIBLE TEXT, NOT THE RAW CONTENT.
        #
        # This is the seam's one subtlety, and getting it wrong made the repair
        # silently dead: with extended thinking enabled the provider returns a
        # LIST of content blocks, not a string, so a check that began
        # `isinstance(text, str)` declined every reply on the only calls that
        # mattered. Biomni flattens those blocks to their text before parsing
        # (a1.py:1306-1322); reading them the same way is what makes this
        # operate on the string biomni is about to see.
        text = _visible(response)
        fixed = repair(text)
        if fixed != text:
            self.repairs += 1
            # Written back as the flattened string. Nothing is lost that
            # survives anyway: biomni rebuilds the message as
            # AIMessage(content=msg) from exactly this text, so the blocks it
            # discards are discarded either way.
            try:
                response.content = fixed
            except (AttributeError, ValueError):
                response = type(response)(content=fixed)

        self._record(response, spent, shape)
        return response

    def __getattr__(self, name):
        # Only reached for attributes this class does not define, so the two
        # above keep their own behaviour and everything else is the real model's.
        return getattr(self._llm, name)

    # -- caching ----------------------------------------------------------- #

    def _prepared(self, messages):
        """The message list with a cache breakpoint after the system prompt.

        The breakpoint goes on the SYSTEM message only. That prefix is the
        stable one -- it is rebuilt from the same string on every call and does
        not vary within a session -- while the conversation after it grows by a
        few hundred characters per turn and would spend a cache write on every
        call to save a cache read on the next.

        A system message that is already a list of content blocks is left
        alone: something else has structured it, and layering a second opinion
        on top of that is how two correct changes make one broken request.
        """
        if not self._cache or not messages:
            return messages
        out = list(messages)
        for i, message in enumerate(out):
            if type(message).__name__ != "SystemMessage":
                continue
            text = getattr(message, "content", None)
            if not isinstance(text, str) or len(text) < CACHEABLE:
                break
            try:
                out[i] = type(message)(content=[{
                    "type": "text",
                    "text": text,
                    "cache_control": {"type": "ephemeral"},
                }])
            except (TypeError, ValueError):
                # The provider's message class would not take blocks. Send what
                # we were given: an uncached call is slower, a failed one is a
                # dead turn.
                pass
            break
        return out

    # -- accounting -------------------------------------------------------- #

    def _record(self, response, spent, shape=None):
        """Read the provider's usage off the response, while it still exists.

        Every field is optional. A provider that reports nothing produces a row
        with a duration and no counts, which is honest -- and is why the counts
        are not defaulted to zero: a zero here would be indistinguishable from
        a real call that read nothing from cache.
        """
        usage = getattr(response, "usage_metadata", None) or {}
        detail = usage.get("input_token_details") or {}
        row = {
            "input": usage.get("input_tokens"),
            "output": usage.get("output_tokens"),
            "cache_write": detail.get("cache_creation"),
            "cache_read": detail.get("cache_read"),
            "seconds": spent,
            # STRUCTURE OF THE REPLY, never its text. What the provider sent
            # back, described: which block types, how many of each, and how
            # many characters each visible one carried. This is here because a
            # measured call reported 6,112 output tokens against 2,348
            # characters of visible answer, and the answer to "where did the
            # rest go" is a property of the response object that stops existing
            # the moment biomni rebuilds the message from its text alone.
            #
            # No block's content is stored. A reasoning block is counted and
            # its type is named; what it says is the model's working and is not
            # this module's to keep, log or surface.
            "shape": shape if shape is not None else _shape(response),
            "stop": (getattr(response, "response_metadata", None) or {})
                    .get("stop_reason"),
        }
        self.calls.append(row)
        if self._telemetry is not None:
            self._telemetry.record("model", spent, **row)

    def raw(self):
        """Every call's accounting, unaggregated, newest last.

        Handed out as-is so an investigation can see per-call numbers rather
        than a sum -- the question "is this one big call or two counted twice"
        is unanswerable from a total.
        """
        return list(self.calls)

    # -- reporting --------------------------------------------------------- #

    def summary(self):
        """Totals over this session's calls, for /telemetry.

        Kept on the wrapper rather than in telemetry.py because these numbers
        exist whether or not telemetry is switched on -- the wrapper is in the
        path either way, and refusing to add them up unless a flag was set
        would make the one number people ask for the one number they cannot get.
        """
        out = {"calls": len(self.calls), "repairs": self.repairs,
               "seconds": 0.0}
        for field in ("input", "output", "cache_write", "cache_read"):
            values = [row[field] for row in self.calls if row[field] is not None]
            out[field] = sum(values) if values else None
        out["seconds"] = sum(row["seconds"] for row in self.calls)
        out["caching"] = self._cache
        return out
