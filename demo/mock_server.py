"""Local stand-ins for Coupa (sandbox, production, dev, qa) and ServiceNow, plus a dashboard.

    python demo/mock_server.py            # http://127.0.0.1:8765

State lives in memory and is reset from demo/data on every start (or POST /reset).
"""

from __future__ import annotations

import copy
import html
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DATA = Path(__file__).parent / "data"
PORT = 8765
STATE: dict = {}


def reset() -> None:
    STATE.clear()
    STATE["coupa"] = {inst: json.loads((DATA / f"coupa-{inst}.json").read_text(encoding="utf-8"))
                      for inst in ("sbx", "prod", "dev", "qa")}
    STATE["sn"] = json.loads((DATA / "servicenow.json").read_text(encoding="utf-8"))
    STATE["next_id"] = 1000


def _match_incidents(query: str) -> list[dict]:
    rows = STATE["sn"]["incidents"]
    if m := re.match(r"number=(\S+)", query):
        return [r for r in rows if r["number"] == m.group(1)]
    if query.startswith("stateIN6,7"):
        words = re.findall(r"short_descriptionLIKE([^\^]+)", query)
        hits = [r for r in rows if r["state"] in ("6", "7")
                and any(w.lower() in r["short_description"].lower() for w in words)]
        return hits
    if m := re.search(r"assignment_group\.name=([^\^]+)", query):
        return [r for r in rows if r["state"] == "1" and r["assignment_group"] == m.group(1)]
    return rows


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, status: int, body: object, content_type: str = "application/json") -> None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
            return {k: v[0] for k, v in parse_qs(raw.decode()).items()}
        return json.loads(raw or b"{}")

    def _route(self, method: str) -> None:
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        params = {k: v[0] for k, v in parse_qs(url.query).items()}

        if method == "GET" and not parts:
            return self._send(200, dashboard(), "text/html; charset=utf-8")
        if method == "POST" and parts == ["reset"]:
            reset()
            return self._send(200, {"ok": True})

        # Coupa: /coupa-<inst>/oauth2/token and /coupa-<inst>/api/<type>[/<id>]
        if parts and parts[0].startswith("coupa-"):
            inst = parts[0][6:]
            if inst not in STATE["coupa"]:
                return self._send(404, {"error": "unknown instance"})
            if parts[1:] == ["oauth2", "token"] and method == "POST":
                form = self._body()
                if form.get("grant_type") != "client_credentials" or form.get("client_secret") != "demo-secret":
                    return self._send(401, {"error": "invalid_client"})
                return self._send(200, {"access_token": f"demo-token-{inst}", "expires_in": 3600})
            # Demo only: play the user administrator, e.g. POST /coupa-qa/demo/users {"login": "apac.controller"}
            if parts[1:] == ["demo", "users"] and method == "POST":
                STATE["next_id"] += 1
                user = {"id": STATE["next_id"], "login": self._body()["login"]}
                STATE["coupa"][inst]["users"].append(user)
                return self._send(201, user)
            # Demo only: someone changes a setting by hand, e.g.
            # POST /coupa-qa/demo/edit {"type": "tax_codes", "match": {"code": "GST5"}, "set": {"percentage": 18}}
            if parts[1:] == ["demo", "edit"] and method == "POST":
                body = self._body()
                for obj in STATE["coupa"][inst].get(body["type"], []):
                    if all(obj.get(k) == v for k, v in body["match"].items()):
                        obj.update(body["set"])
                        return self._send(200, obj)
                return self._send(404, {"error": "no matching object"})
            if self.headers.get("Authorization") != f"Bearer demo-token-{inst}":
                return self._send(401, {"error": "unauthorized"})
            if len(parts) >= 3 and parts[1] == "api":
                objects = STATE["coupa"][inst].get(parts[2])
                if objects is None:
                    return self._send(404, {"error": f"no such resource {parts[2]}"})
                if method == "GET" and len(parts) == 3:
                    offset, limit = int(params.get("offset", 0)), int(params.get("limit", 50))
                    return self._send(200, objects[offset:offset + limit])
                if method == "POST" and len(parts) == 3:
                    obj = self._body()
                    STATE["next_id"] += 1
                    obj["id"] = STATE["next_id"]
                    objects.append(obj)
                    return self._send(201, obj)
                if method == "PUT" and len(parts) == 4:
                    for i, existing in enumerate(objects):
                        if str(existing.get("id")) == parts[3]:
                            objects[i] = {**self._body(), "id": existing["id"]}
                            return self._send(200, objects[i])
                    return self._send(404, {"error": "not found"})
            return self._send(404, {"error": "not found"})

        # ServiceNow: /sn/api/now/table/<table>[/<sys_id>]
        if parts[:4] == ["sn", "api", "now", "table"] and len(parts) >= 5:
            table = parts[4]
            if table == "incident" and method == "GET":
                rows = _match_incidents(params.get("sysparm_query", ""))[: int(params.get("sysparm_limit", 20))]
                return self._send(200, {"result": rows})
            if table == "sys_user_group" and method == "GET":
                name = params.get("sysparm_query", "").removeprefix("name=")
                if name in STATE["sn"]["groups"]:
                    return self._send(200, {"result": [{"sys_id": f"grp-{name}", "name": name}]})
                return self._send(200, {"result": []})
            if table == "incident" and method == "PATCH" and len(parts) == 6:
                for row in STATE["sn"]["incidents"]:
                    if row["sys_id"] == parts[5]:
                        body = self._body()
                        if "work_notes" in body:
                            row["work_notes"] = (body["work_notes"] + "\n\n" + row.get("work_notes", "")).strip()
                        if "assignment_group" in body:
                            row["assignment_group"] = body["assignment_group"].removeprefix("grp-")
                        return self._send(200, {"result": row})
                return self._send(404, {"error": "not found"})
        return self._send(404, {"error": "not found"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_PATCH(self):
        self._route("PATCH")


def dashboard() -> str:
    def esc(v) -> str:
        return html.escape(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))

    incidents = "".join(
        f"<tr><td><b>{esc(r['number'])}</b></td><td>{esc(r['short_description'])}</td>"
        f"<td>{esc(r['assignment_group'])}</td><td><pre>{esc(r.get('work_notes') or '-')}</pre></td></tr>"
        for r in STATE["sn"]["incidents"] if r["state"] == "1")
    coupa = []
    for inst, types in STATE["coupa"].items():
        rows = "".join(
            f"<tr><td>{esc(t)}</td><td>{esc(o.get('name') or o.get('code') or o.get('login') or o.get('id'))}</td>"
            f"<td><pre>{esc({k: v for k, v in o.items() if k != 'id'})}</pre></td></tr>"
            for t, objs in types.items() for o in objs)
        coupa.append(f"<h3>Coupa {inst}</h3><table><tr><th>Type</th><th>Key</th><th>Data</th></tr>{rows}</table>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>P2P Demo Systems</title><meta http-equiv="refresh" content="5">
<style>body{{font:14px system-ui,sans-serif;margin:16px;background:#f7f7f9;color:#222}}
table{{border-collapse:collapse;width:100%;background:#fff;margin-bottom:24px}}
td,th{{border:1px solid #ddd;padding:6px;vertical-align:top;text-align:left}}th{{background:#eef}}
pre{{margin:0;white-space:pre-wrap;font-size:12px}}h2{{margin-top:28px}}</style></head><body>
<h1>P2P Demo Systems</h1><p>Mock Coupa and ServiceNow for testing. Refreshes every 5 seconds.</p>
<h2>ServiceNow: open incidents</h2><table><tr><th>Number</th><th>Short description</th>
<th>Assignment group</th><th>Work notes</th></tr>{incidents}</table>
<h2>Coupa configuration</h2>{''.join(coupa)}</body></html>"""


if __name__ == "__main__":
    reset()
    print(f"Mock Coupa + ServiceNow on http://127.0.0.1:{PORT}  (dashboard at /)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
