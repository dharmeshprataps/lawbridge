"""Small localhost JSON REST server for the expense store."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import socket
import sqlite3
from urllib.parse import unquote, urlsplit

from .store import APIError, ExpenseStore, json_safe

MAX_BODY = 1024 * 1024


def _duplicate_checked_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_number(value):
    raise ValueError("JSON numbers must be exact integers; floats and constants are unsupported")


class ExpenseHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


def create_server(store: ExpenseStore, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SplitExpense/0.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, format, *args):
            return

        def _json(self, status: int, body: dict):
            payload = (json.dumps(body, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _body(self):
            if self.headers.get("Transfer-Encoding"):
                raise APIError(400, "invalid_request", "Chunked request bodies are unsupported")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None:
                raise APIError(400, "invalid_request", "One valid Content-Length header is required")
            if len(lengths[0]) > 16:
                raise APIError(400, "invalid_request", "Request body length is unsupported")
            length = int(lengths[0])
            if length > MAX_BODY:
                raise APIError(400, "invalid_request", "Request body exceeds 1 MiB")
            content_type = self.headers.get("Content-Type", "application/json").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise APIError(400, "invalid_request", "Content-Type must be application/json")
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("Incomplete request body")
                body = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_checked_object,
                                  parse_float=_reject_number, parse_constant=_reject_number)
                json_safe(body)
                return body
            except (ValueError, UnicodeError, RecursionError, socket.timeout) as exc:
                raise APIError(400, "invalid_json", str(exc)) from exc

        def _route(self, method):
            path = urlsplit(self.path).path
            parts = [unquote(part) for part in path.strip("/").split("/")] if path.strip("/") else []
            if method == "GET" and parts == ["health"]:
                return 200, {"status": "ok"}
            if parts == ["groups"]:
                if method == "GET":
                    return 200, store.list_groups()
                return store.create_group(self._body())
            if len(parts) >= 2 and parts[0] == "groups":
                group_id = parts[1]
                if method == "GET":
                    if len(parts) == 2:
                        return 200, store.snapshot(group_id)
                    if len(parts) == 3:
                        functions = {"balances": store.balances, "expenses": store.list_expenses,
                                     "settlements": store.list_settlements}
                        if parts[2] in functions:
                            return 200, functions[parts[2]](group_id)
                elif len(parts) == 3:
                    if parts[2] == "expenses":
                        return store.add_expense(group_id, self._body())
                    if parts[2] == "settlements":
                        return store.add_settlement(group_id, self._body())
            raise APIError(404, "not_found", "Route does not exist")

        def _dispatch(self, method):
            try:
                status, body = self._route(method)
            except APIError as exc:
                status, body = exc.status, exc.body
            except sqlite3.Error:
                status, body = 503, {"error": {"code": "storage_unavailable", "message": "Storage operation could not complete"}}
            try:
                self._json(status, body)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _unsupported(self):
            self._json(405, {"error": {"code": "method_not_allowed", "message": "Use GET or POST"}})

        do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _unsupported

    return ExpenseHTTPServer((host, port), Handler)
