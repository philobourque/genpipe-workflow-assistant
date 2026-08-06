"""Optional counters for the generate/execute loop.

Recording costs a dict append per graph-node call and per checkpoint write --
cheap, but not free, and a report nobody reads is not worth even that in a
production conversation. Off by default; turn it on for an evaluation run
with GENPIPE_TELEMETRY=1 (or Telemetry(enabled=True) directly) and read
agent.telemetry.summary() afterwards, or type /telemetry in the REPL.

This does not attempt to measure token counts or context size in bytes --
Biomni's nodes do not expose the token accounting the model API returns, and
guessing at it from message length would be a number that looks precise and
is not. What it measures is real: wall time, and how many times each node in
the graph actually ran, which is the thing this codebase's own audit
(AGENT-FIXES.md) identified as the visible cost -- one full model inference
per <execute> block, however small.
"""
import os
import time


class Telemetry:
    """A flat log of (kind, duration, extra) tuples for one process's runs.

    Not per-turn: calls accumulate across turns until reset() is asked for,
    because comparing "the last turn" to "the whole session so far" is often
    exactly the comparison an evaluation run wants.
    """

    def __init__(self, enabled=None):
        self.enabled = (os.environ.get("GENPIPE_TELEMETRY", "") not in ("", "0")
                        if enabled is None else enabled)
        self.reset()

    def reset(self):
        self.calls = []          # [(kind, duration_seconds, extra_dict), ...]
        self._turn_started = None

    def start_turn(self):
        if self.enabled:
            self._turn_started = time.time()

    def end_turn(self):
        """Duration of the turn just finished, or None if telemetry is off."""
        if not self.enabled or self._turn_started is None:
            return None
        elapsed = time.time() - self._turn_started
        self._turn_started = None
        self.record("turn", elapsed)
        return elapsed

    def record(self, kind, duration, **extra):
        if self.enabled:
            self.calls.append((kind, duration, extra))

    def timed(self, kind, fn, *args, **kwargs):
        """Call fn(*args, **kwargs), recording its duration under `kind`."""
        if not self.enabled:
            return fn(*args, **kwargs)
        started = time.time()
        try:
            return fn(*args, **kwargs)
        finally:
            self.record(kind, time.time() - started)

    def summary(self):
        """Count, total, and mean duration per call kind.

        {"generate": {"count": 4, "total": 9.2, "mean": 2.3}, "execute": {...}}
        Empty when nothing was recorded, which is also the "telemetry is off"
        case -- callers don't need to check .enabled separately.
        """
        out = {}
        for kind, duration, _ in self.calls:
            row = out.setdefault(kind, {"count": 0, "total": 0.0})
            row["count"] += 1
            row["total"] += duration
        for row in out.values():
            row["mean"] = row["total"] / row["count"]
        return out
