"""HTTP API (standard library only: no web framework to install, and it runs identically locally and in a container).

Endpoints
  GET  /health
  GET  /cases
  POST /cases/{case_id}/analyze
  GET  /runs/{run_id}
  GET  /proposals                    GET /proposals/{id}              GET /proposals/{id}/memo
  POST /proposals/{id}/approvals     {"role": "underwriter"}          approver identity: see below
  POST /approvals/{approval_id}/revoke
  POST /proposals/{id}/execute
  GET  /audit?limit=50               GET /audit/verify
  GET  /search?kind=clause|regulation|ruling&q=...&vendor=&port=&as_of=

Security model
  * If GLCC_API_KEY is set, every endpoint except /health needs the header  X-API-Key  (compared in constant time).
  * Approver identity comes from the header X-MS-CLIENT-PRINCIPAL-NAME, which Azure Container Apps authentication
    (Entra ID, "Easy Auth") sets AFTER authenticating the user. A body field "approver_id" is accepted ONLY when
    GLCC_ALLOW_BODY_IDENTITY=true (local demos), because anyone can type any name into a request body.
  * The role an approver may use is checked against the role directory, not against the request.
  * Request bodies are capped at 64 KB; every error is a JSON object; internal exceptions are logged, not echoed.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..governance.store import StoreError
from ..ingest.common import IngestError
from ..service import ExceptionService

log = logging.getLogger("glcc.api")
MAX_BODY = 64 * 1024
IDENTITY_HEADER = "X-MS-CLIENT-PRINCIPAL-NAME"


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def make_server(service: ExceptionService, host: str = "127.0.0.1", port: int = 8000, api_key: str | None = None,
                allow_body_identity: bool = False) -> ThreadingHTTPServer:

    class Handler(BaseHTTPRequestHandler):
        server_version = "glcc-exceptions/1.0"

        # ---- plumbing -------------------------------------------------------------------------------------------------
        def log_message(self, fmt, *args):                       # route access logs through logging, not stderr
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body, content_type: str = "application/json") -> None:
            data = (body if isinstance(body, str) else json.dumps(body, default=str)).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, f"request body larger than {MAX_BODY} bytes")
            if length == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ApiError(400, "request body is not valid JSON") from exc
            if not isinstance(data, dict):
                raise ApiError(400, "request body must be a JSON object")
            return data

        def _authenticate(self, path: str) -> None:
            if api_key and path != "/health":
                supplied = self.headers.get("X-API-Key", "")
                if not hmac.compare_digest(supplied.encode("utf-8"), api_key.encode("utf-8")):
                    raise ApiError(401, "missing or invalid API key")

        def _identity(self, body: dict) -> str:
            who = (self.headers.get(IDENTITY_HEADER) or "").strip()
            if not who and allow_body_identity:
                who = str(body.get("approver_id", "")).strip()
            if not who:
                raise ApiError(401, f"no authenticated identity: expected the {IDENTITY_HEADER} header")
            return who

        def _dispatch(self, method: str) -> None:
            try:
                url = urlparse(self.path)
                path, query = url.path.rstrip("/") or "/", {k: v[0] for k, v in parse_qs(url.query).items()}
                self._authenticate(path)
                status, payload, ctype = self._route(method, path, query)
                self._send(status, payload, ctype)
            except ApiError as exc:
                self._send(exc.status, {"error": exc.message, **exc.extra})
            except StoreError as exc:
                self._send(409, {"error": str(exc)})
            except IngestError as exc:
                self._send(404, {"error": str(exc)})
            except Exception:                                    # never leak internals; log them for operators
                log.exception("unhandled error for %s %s", method, self.path)
                self._send(500, {"error": "internal error"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        # ---- routes -------------------------------------------------------------------------------------------------------
        def _route(self, method: str, path: str, q: dict):
            s = service
            if method == "GET" and path == "/health":
                return 200, {"status": "ok", "mode": "llm" if s.llm else "offline", "corpus": s.corpus.stats(), "audit": s.verify_audit()["valid"]}, "application/json"
            if method == "GET" and path == "/cases":
                return 200, {"cases": s.corpus.case_ids()}, "application/json"

            m = re.fullmatch(r"/cases/([A-Za-z0-9_\-]+)/analyze", path)
            if method == "POST" and m:
                run = s.analyze(m.group(1), actor=self._actor())
                if run["status"] == "failed":
                    raise ApiError(404 if "unknown case" in (run["error"] or "") else 422, run["error"], run_id=run["run_id"])
                return 200, s.summarize_run(run), "application/json"

            m = re.fullmatch(r"/runs/([A-Za-z0-9\-]+)", path)
            if method == "GET" and m:
                run = s.store.get_run(m.group(1))
                if not run:
                    raise ApiError(404, "unknown run")
                return 200, run, "application/json"

            if method == "GET" and path == "/proposals":
                return 200, {"proposals": [{"proposal_id": p["proposal_id"], "case_id": p["content"]["case_id"], "decision": p["content"]["decision_type"],
                                            "amount": p["content"]["amount"], "status": p["status"]} for p in s.store.list_proposals()]}, "application/json"

            m = re.fullmatch(r"/proposals/(PROP-[0-9a-f]{10})(/memo|/approvals|/execute)?", path)
            if m:
                pid, sub = m.group(1), m.group(2)
                if method == "GET" and sub is None:
                    view = s.proposal_view(pid)
                    if not view:
                        raise ApiError(404, "unknown proposal")
                    return 200, view, "application/json"
                if method == "GET" and sub == "/memo":
                    memo = s.memo(pid)
                    if memo is None:
                        raise ApiError(404, "no memo for this proposal")
                    return 200, memo, "text/markdown"
                if method == "POST" and sub == "/approvals":
                    body = self._body()
                    role = str(body.get("role", "")).strip()
                    if not role:
                        raise ApiError(400, "role is required")
                    return 201, s.approve(pid, role, self._identity(body)), "application/json"
                if method == "POST" and sub == "/execute":
                    body = self._body()
                    result = s.execute(pid, f"user:{self._identity(body)}")
                    return {"executed": 200, "failed": 502, "blocked": 409}.get(result["status"], 200), result, "application/json"

            m = re.fullmatch(r"/approvals/(\d+)/revoke", path)
            if method == "POST" and m:
                body = self._body()
                return 200, s.revoke(int(m.group(1)), self._identity(body)), "application/json"

            if method == "GET" and path == "/audit":
                limit = min(max(int(q.get("limit", 50) or 50), 1), 1000)
                return 200, {"records": s.store.audit_records(limit=limit)}, "application/json"
            if method == "GET" and path == "/audit/verify":
                return 200, s.verify_audit(), "application/json"
            if method == "GET" and path == "/search":
                if not q.get("q"):
                    raise ApiError(400, "q is required")
                try:
                    results = s.search(q.get("kind", "clause"), q["q"], q.get("vendor", "any"), q.get("port", "any"), q.get("as_of", "2025-01-01"))
                except ValueError as exc:
                    raise ApiError(400, str(exc)) from exc
                return 200, {"results": results}, "application/json"
            raise ApiError(404, "not found")

        def _actor(self) -> str:
            return f"api:{(self.headers.get(IDENTITY_HEADER) or 'service').strip()}"

    return ThreadingHTTPServer((host, port), Handler)
