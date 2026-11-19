"""Quorum Web: a local browser UI for Quorum discussions.

Runs the same discussion engine as the MCP plugin (agent CLIs on your own subscriptions:
claude, codex, agy, grok; plus any API providers from ~/.quorum/.env) and serves a single
page at http://127.0.0.1:8765. Listens on localhost only.

    python webui/server.py [--port 8765] [--no-browser]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Use the quorum package from this checkout, wherever the folder lives.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from quorum import mcp as qm
from quorum.config import CACHE_DIR

HERE = Path(__file__).resolve().parent
# Participants run (read-only) inside this empty folder, so they never see your other files.
WORK_DIR = CACHE_DIR / "web-workdir"
HISTORY_FILE = CACHE_DIR / "web-history.json"


def _load_history() -> list[dict]:
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save_history(items: list[dict]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def _error(e: Exception, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": str(e) or type(e).__name__}, status_code=status)


async def index(_: Request) -> FileResponse:
    return FileResponse(HERE / "index.html")


async def models(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await qm._list_models({"refresh": request.query_params.get("refresh") == "1"}))
    except Exception as e:
        return _error(e, 500)


# Follow-ups: Quorum runs one question per discussion, so a reply in a thread starts a new
# discussion whose question carries the earlier turns (the user's messages and the group's
# final answers; not the full debates, which would blow up the prompt).
MAX_CONTEXT_TURNS = 6
MAX_ANSWER_CHARS = 8000


def _thread_id(entry: dict) -> str:
    return entry.get("thread") or entry["id"]


def _run_messages(run_id: str) -> list[dict]:
    run = qm.RUNS.get(run_id)
    if run is not None:
        return run.messages
    try:
        return json.loads((qm.RUNS_DIR / f"{run_id}.json").read_text(encoding="utf-8"))["messages"]
    except (OSError, ValueError, KeyError):
        return []


def _chain(history: list[dict], last_id: str) -> list[dict]:
    """The thread's turns from the first one up to and including last_id."""
    by_id = {h["id"]: h for h in history}
    chain, cur = [], by_id.get(last_id)
    while cur is not None and len(chain) < 200:
        chain.append(cur)
        cur = by_id.get(cur.get("parent"))
    return chain[::-1]


def _followup_question(chain: list[dict], message: str) -> str:
    shown = chain[-MAX_CONTEXT_TURNS:]
    parts = ["This is a follow-up in an ongoing conversation between the user and your discussion "
             "group. The earlier turns are below, each with the final answer your group gave."]
    if len(chain) > len(shown):
        parts.append(f"({len(chain) - len(shown)} earlier turns are left out.)")
    for n, h in enumerate(shown, len(chain) - len(shown) + 1):
        synth = next((m for m in _run_messages(h["id"]) if m.get("type") == "SynthesisResult"), None)
        answer = (synth or {}).get("synthesis") or "(No final answer: that discussion did not finish.)"
        if len(answer) > MAX_ANSWER_CHARS:
            answer = answer[:MAX_ANSWER_CHARS] + "\n[…]"
        parts.append(f"### Turn {n}: the user wrote\n{h['question']}\n\n### Turn {n}: your group's final answer\n{answer}")
    parts.append(
        f"### Now the user writes\n{message}\n\n"
        "Respond to this new message. If it is feedback, use it: correct or revise the earlier answer "
        "where it asks for that and keep what still holds. If it is a new question, answer it in the "
        "context of this conversation. Reply in the language of the user's messages."
    )
    return "\n\n".join(parts)


async def start_run(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:  # includes undecodable (non-UTF-8) bodies
        return _error(ValueError("The request is not valid UTF-8 JSON."))
    message = str(body.get("question") or "").strip()
    if not message:
        return _error(ValueError("Type a question first."))
    history = _load_history()
    parent_id = body.get("parent_id")
    question, extra = message, {}
    if parent_id:
        chain = _chain(history, parent_id)
        if not chain:
            return _error(ValueError("The discussion you are replying to no longer exists."))
        if any(qm.RUNS.get(h["id"]) and qm.RUNS[h["id"]].status == "running" for h in chain):
            return _error(ValueError("Wait for the current answer before replying."))
        question = _followup_question(chain, message)
        extra = {"parent": parent_id, "thread": _thread_id(chain[0])}
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        res = await qm._start({
            "question": question,
            "participants": body.get("participants") or [],
            "method": body.get("method") or "standard",
            "cwd": str(WORK_DIR),
        })
    except Exception as e:
        return _error(e)
    history = _load_history()
    history.insert(0, {"id": res["run_id"], "question": message, "method": res["method"],
                       "participants": res["participants"], "started": time.time(), **extra})
    _save_history(history[:1000])
    return JSONResponse(res)


def _history_entry(run_id: str) -> dict:
    return next((h for h in _load_history() if h["id"] == run_id), {})


async def get_run(request: Request) -> JSONResponse:
    run_id = request.path_params["run_id"]
    since = int(request.query_params.get("since", "0"))
    entry = _history_entry(run_id)
    run = qm.RUNS.get(run_id)
    if run is not None:
        status, messages = qm._status(run, full=False), run.messages
    else:
        try:
            saved = json.loads((qm.RUNS_DIR / f"{run_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if not entry:
                return _error(KeyError(f"Unknown run {run_id}"), 404)
            # Started, but the server stopped before it finished.
            saved = {"run": {"run_id": run_id, "status": "failed", "participants": entry["participants"],
                             "error": "The app was closed before this discussion finished."},
                     "messages": []}
        status, messages = saved["run"], saved["messages"]
    return JSONResponse({**status, "question": entry.get("question", ""), "method": entry.get("method"),
                         "messages": messages[since:], "next": len(messages)})


async def cancel_run(request: Request) -> JSONResponse:
    run = qm.RUNS.get(request.path_params["run_id"])
    if run is None or run.status != "running" or run.task is None:
        return _error(ValueError("That discussion is not running."))
    run.status, run.error = "failed", "Stopped by you."
    run.task.cancel()
    return JSONResponse({"status": "cancelled"})


async def history(_: Request) -> JSONResponse:
    items = _load_history()
    for h in items:
        run = qm.RUNS.get(h["id"])
        h["running"] = bool(run and run.status == "running")
        h["thread"] = _thread_id(h)
    return JSONResponse(items)


async def delete_history(request: Request) -> JSONResponse:
    """Delete a whole thread (the id may be any turn in it)."""
    run_id = request.path_params["run_id"]
    history = _load_history()
    entry = next((h for h in history if h["id"] == run_id), None)
    thread = _thread_id(entry) if entry else run_id
    ids = {h["id"] for h in history if _thread_id(h) == thread} | {run_id}
    if any(qm.RUNS.get(i) and qm.RUNS[i].status == "running" for i in ids):
        return _error(ValueError("Stop the discussion before deleting it."))
    _save_history([h for h in history if h["id"] not in ids])
    for i in ids:
        (qm.RUNS_DIR / f"{i}.json").unlink(missing_ok=True)
        qm.RUNS.pop(i, None)
    return JSONResponse({"status": "deleted", "deleted": sorted(ids)})


app = Starlette(routes=[
    Route("/", index),
    Route("/api/models", models),
    Route("/api/runs", start_run, methods=["POST"]),
    Route("/api/runs/{run_id}", get_run),
    Route("/api/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
    Route("/api/history", history),
    Route("/api/history/{run_id}", delete_history, methods=["DELETE"]),
])


def main() -> None:
    parser = argparse.ArgumentParser(description="Quorum Web UI")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1",
                        help="Address to listen on; 0.0.0.0 only inside a container (see docker-compose.yml).")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    print(f"\n  Quorum Web is running at {url}\n  Close this window to stop it.\n", flush=True)
    if not args.no_browser:
        threading.Timer(1.2, webbrowser.open, [url]).start()
    # Our own event loop (the default proactor loop on Windows), which subprocesses need.
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
