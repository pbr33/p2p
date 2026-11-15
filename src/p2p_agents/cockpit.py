"""Go-Live Cockpit: a local web page for the whole team, with the Copilot built in.

    p2p cockpit            → http://127.0.0.1:8770

Shows every roll-out: readiness score, DEV → QA pipeline with test results, what blocks
go-live, UAT progress (from the results testers type into uat-scripts.csv), manual steps
(tick them off here) and the configuration checklist. "Re-test" re-reads an environment
and runs the tests again without changing it, so drift shows up. The Copilot panel answers
questions and prepares changes; a system change only runs after someone clicks Approve.

Standard library only; binds to 127.0.0.1 by default (no authentication — do not expose it).
"""

from __future__ import annotations

import base64
import html
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from p2p_agents.agents.copilot import CopilotAgent, NoArgs, VerifyArgs
from p2p_agents.llm import LLM
from p2p_agents.report import ai_summary, gather, render
from p2p_agents.rollout.consistency import consistency
from p2p_agents.rollout.documents import SUPPORTED
from p2p_agents.rollout.reports import set_manual_done
from p2p_agents.settings import CONFIG_DIR, load_instances
from p2p_agents.watch import check_drift, read_watch
from p2p_agents.workspace import RUNS_DIR, all_runs, load_run, roi, run_summary

PAGE = Path(__file__).with_name("cockpit.html")


class Cockpit:
    def __init__(self, config_dir: Path = CONFIG_DIR, runs_root: Path = RUNS_DIR, copilot: CopilotAgent | None = None,
                 watch_every: int = 0):
        self.config_dir = config_dir
        self.runs_root = runs_root
        # Changes requested in the chat wait for the Approve button
        self.copilot = copilot or CopilotAgent(approve=lambda summary: None, config_dir=config_dir, runs_root=runs_root)
        self.lock = threading.Lock()  # one copilot conversation, one change at a time
        self.watch_every = watch_every
        if watch_every:
            threading.Thread(target=self._watch_loop, daemon=True).start()

    def _watch_loop(self) -> None:
        stop = threading.Event()
        while not stop.wait(self.watch_every * 60):
            try:
                with self.lock:
                    check_drift(self.runs_root, self.config_dir)
            except Exception:  # noqa: BLE001 - keep watching
                logging.getLogger(__name__).exception("drift check failed")

    def report(self, with_ai: bool) -> str:
        data = gather(self.runs_root, self.config_dir)
        summary = error = None
        if with_ai:
            try:
                summary = ai_summary(data, self.copilot.llm or LLM(effort="medium"))
            except Exception as e:  # noqa: BLE001 - the pack is still useful without it
                error = _friendly(e)
        return render(data, summary, error)

    # ───── API ─────

    def handle(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        parts = [unquote(p) for p in path.strip("/").split("/")]
        if method == "GET" and path == "/api/runs":
            return 200, all_runs(self.runs_root)
        if method == "GET" and path == "/api/envs":
            return 200, [{"name": n, "platform": i.platform, "production": i.is_production}
                         for n, i in load_instances(self.config_dir).items()]
        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "runs"]:
            run_dir, state = load_run(parts[2], self.runs_root)
            return 200, run_summary(run_dir, state, detail=True)
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "verify":
            with self.lock:
                return 200, self.copilot.t_verify(VerifyArgs(run=parts[2], instance=str(body.get("instance", ""))))
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "manual":
            run_dir, _ = load_run(parts[2], self.runs_root)
            if not re.fullmatch(r"M\d+", str(body.get("step", ""))):
                return 400, {"error": "step must look like M1"}
            return 200, set_manual_done(run_dir, body["step"], bool(body.get("done", True)), str(body.get("by", ""))[:60])
        if method == "POST" and path == "/api/ask":
            message = str(body.get("message", "")).strip()
            if not message:
                return 400, {"error": "empty message"}
            with self.lock:
                try:
                    reply = self.copilot.ask(message[:4000])
                except Exception as e:  # noqa: BLE001 - shown in the chat
                    return 200, {"error": _friendly(e), "approvals": self._approvals()}
            return 200, {"reply": reply, "approvals": self._approvals()}
        if method == "GET" and path == "/api/consistency":
            return 200, consistency(self.runs_root, self.config_dir)
        if method == "GET" and path == "/api/watch":
            return 200, {**read_watch(self.runs_root), "every_minutes": self.watch_every}
        if method == "POST" and path == "/api/watch":
            with self.lock:
                return 200, check_drift(self.runs_root, self.config_dir)
        if method == "GET" and path == "/api/roi":
            return 200, roi(self.runs_root, self.config_dir)
        if method == "GET" and path == "/api/templates":
            return 200, self.copilot.t_templates(NoArgs())
        if method == "POST" and path == "/api/document":
            return 200, self._document(body)
        if method == "GET" and path == "/api/approvals":
            return 200, self._approvals()
        if method == "POST" and len(parts) == 3 and parts[:2] == ["api", "approvals"]:
            with self.lock:
                return 200, self.copilot.decide(parts[2], bool(body.get("approve")))
        if method == "POST" and path == "/api/chat/reset":
            self.copilot.reset()
            return 200, {"ok": True}
        return 404, {"error": f"no route {method} {path}"}

    def _document(self, body: dict[str, Any]) -> dict[str, Any]:
        """Uploaded client document → draft answers file (rollout/answers/<name>.csv) + open points."""
        name = Path(str(body.get("filename", ""))).name
        if not name or Path(name).suffix.lower() not in SUPPORTED:
            raise ValueError(f"upload one of: {', '.join(SUPPORTED)}")
        try:
            data = base64.b64decode(str(body.get("content", "")), validate=True)
        except ValueError:
            raise ValueError("file content must be base64") from None
        if not data or len(data) > 15_000_000:
            raise ValueError("the file is empty or larger than 15 MB")
        uploads = self.runs_root / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        saved = uploads / name
        saved.write_bytes(data)
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(name).stem).strip("-").lower() or "client"
        answers = self.runs_root / "answers" / f"{stem}.csv"
        with self.lock:
            try:
                ex = self.copilot.document_to_answers(str(body.get("template", "")), [saved], answers)
            except Exception as e:  # noqa: BLE001 - shown on the page
                import anthropic

                if isinstance(e, anthropic.APIError):
                    raise RuntimeError(_friendly(e)) from e
                raise
        return {**ex.model_dump(mode="json"), "answers_file": _rel(answers), "gaps_file": _rel(answers.with_suffix(".gaps.md"))}

    def _approvals(self) -> list[dict[str, str]]:
        return [{"id": p.id, "summary": p.summary} for p in self.copilot.pending.values()]


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def _friendly(e: Exception) -> str:
    try:
        import anthropic

        if isinstance(e, anthropic.AuthenticationError):
            return "The Copilot needs a valid Anthropic API key (ANTHROPIC_API_KEY, e.g. in demo\\.env). Everything else on this page works without it."
        if isinstance(e, anthropic.APIConnectionError):
            return "Could not reach the Claude API — check the network or proxy."
        if isinstance(e, anthropic.APIError):
            if "credit balance" in str(e):
                return ("The Anthropic account has no credit left. Add credit at console.anthropic.com → "
                        "Plans & Billing, then ask again. Everything else on this page works without it.")
            return f"Claude API error: {getattr(e, 'message', e)}"
    except ImportError:
        pass
    return str(e)


def make_handler(cockpit: Cockpit) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # quiet
            pass

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?")[0]
            if method == "GET" and path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if method == "GET" and path == "/report":
                try:
                    page = cockpit.report(with_ai="ai=0" not in self.path)
                except Exception as e:  # noqa: BLE001
                    return self._send(500, f"<p>Could not build the pack: {html.escape(str(e))}</p>".encode(),
                                      "text/html; charset=utf-8")
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            body: dict[str, Any] = {}
            if method == "POST":
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    return self._send(415, {"error": "send application/json"})  # also blocks simple cross-site posts
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "invalid JSON"})
            try:
                status, payload = cockpit.handle(method, path, body)
            except (LookupError, ValueError, FileNotFoundError) as e:
                status, payload = 400, {"error": str(e)}
            except Exception as e:  # noqa: BLE001
                status, payload = 500, {"error": _friendly(e)}
            self._send(status, payload)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8770, cockpit: Cockpit | None = None, watch_every: int = 0) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(cockpit or Cockpit(watch_every=watch_every)))
    print(f"Go-Live Cockpit on http://{host}:{port}  (Ctrl+C to stop)"
          + (f"; drift watch every {watch_every} min" if watch_every else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
