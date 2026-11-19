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


async def start_run(request: Request) -> JSONResponse:
    body = await request.json()
    question = str(body.get("question") or "").strip()
    if not question:
        return _error(ValueError("Type a question first."))
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
    history.insert(0, {"id": res["run_id"], "question": question, "method": res["method"],
                       "participants": res["participants"], "started": time.time()})
    _save_history(history[:500])
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
    return JSONResponse(items)


async def delete_history(request: Request) -> JSONResponse:
    run_id = request.path_params["run_id"]
    run = qm.RUNS.get(run_id)
    if run and run.status == "running":
        return _error(ValueError("Stop the discussion before deleting it."))
    _save_history([h for h in _load_history() if h["id"] != run_id])
    (qm.RUNS_DIR / f"{run_id}.json").unlink(missing_ok=True)
    qm.RUNS.pop(run_id, None)
    return JSONResponse({"status": "deleted"})


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
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    print(f"\n  Quorum Web is running at {url}\n  Close this window to stop it.\n", flush=True)
    if not args.no_browser:
        threading.Timer(1.2, webbrowser.open, [url]).start()
    # Our own event loop (the default proactor loop on Windows), which subprocesses need.
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
