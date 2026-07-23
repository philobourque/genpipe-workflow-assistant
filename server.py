"""
Thin web wrapper around your existing GenPipes agent.

It does not change the agent. It builds the one you already configure in
launch_agent.py, runs agent.go(task) in a background thread, captures
everything the agent prints, and streams it to the browser line by line. One
agent, one run at a time, single user.

WARNING: agent.go() is Biomni's own ungated loop, NOT GenpipeA1's gated
run()/resume(). This UI does not stop for approval before a submission
reaches the cluster. It is a convenience face for generation, exploration, and
demos -- do not point it at a real submission task. Use the CLI
(start_agent.sh -> agent.run(...)/agent.resume(...)) for anything that
actually submits to the scheduler, until this UI grows its own gate.

The ONLY integration point is build_agent() below, imported from
launch_agent.py. Building the agent here runs your normal setup once (the big
software dump prints to this console at startup, not into a run).
"""

import asyncio
import contextlib
import io
import json
import queue
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# --- the one integration point -------------------------------------------
from launch_agent import build_agent, _require_api_key
_require_api_key()
agent = build_agent()
# -------------------------------------------------------------------------

app = FastAPI()
HERE = Path(__file__).parent

# Single-instance reality: one agent, one thread of execution. We serialize
# runs with a lock and refuse a second concurrent run rather than corrupt the
# shared memory thread.
_lock = threading.Lock()
_state = {"busy": False, "q": None}
_history = []  # session-only list of {"task":..., "answer":...}


class Task(BaseModel):
    task: str


class _Capture(io.TextIOBase):
    """Pushes every chunk the agent prints onto a queue."""

    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)
        return len(s)


def _run(task: str, q: "queue.Queue"):
    cap = _Capture(q)
    answer = None
    try:
        # Capture stdout AND stderr: Biomni prints the loop to stdout, but
        # some libraries log to stderr. We want both in the feed.
        with contextlib.redirect_stdout(cap), contextlib.redirect_stderr(cap):
            result = agent.go(task)
        # go() return shape varies across Biomni versions. Take the last
        # string-ish thing as the final answer if we can; otherwise the
        # streamed log already showed everything.
        if isinstance(result, tuple) and result:
            answer = result[-1]
        elif isinstance(result, str):
            answer = result
    except Exception as e:  # surface failures into the feed, don't swallow
        q.put(f"\n[run failed] {type(e).__name__}: {e}\n")
    finally:
        _history.append({"task": task, "answer": answer if isinstance(answer, str) else ""})
        q.put(None)  # sentinel: run complete
        _state["busy"] = False


@app.post("/run")
def run(t: Task):
    task = t.task.strip()
    if not task:
        return {"ok": False, "error": "Empty task."}
    with _lock:
        if _state["busy"]:
            return {"ok": False, "error": "A run is already in progress."}
        _state["busy"] = True
        q = queue.Queue()
        _state["q"] = q
    threading.Thread(target=_run, args=(task, q), daemon=True).start()
    return {"ok": True}


@app.get("/stream")
async def stream():
    q = _state["q"]
    if q is None:
        return StreamingResponse(iter(["event: done\ndata: \n\n"]),
                                 media_type="text/event-stream")

    async def gen():
        loop = asyncio.get_event_loop()
        while True:
            chunk = await loop.run_in_executor(None, q.get)
            if chunk is None:
                yield "event: done\ndata: \n\n"
                break
            yield f"data: {json.dumps(chunk)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/history")
def history():
    return {"history": _history}


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")
