"""HTTP entry point: JSON API + static page, standard library only."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import matching
from . import samples as S
from . import service
from . import store

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
MAX_BODY = 12 * 1024 * 1024
ACTOR = os.environ.get("LINKER_ACTOR", "operator")
API_VERSION = "1.0.0"

STATIC_ROUTES = {"/": "index.html", "/index.html": "index.html"}


def _json_bytes(payload: Any, status: int = 200,
                extra_headers: dict[str, str] | None = None) -> tuple[int, list[tuple[str, str]], bytes]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ]
    for key, value in (extra_headers or {}).items():
        headers.append((key, value))
    return status, headers, body


class Handler(BaseHTTPRequestHandler):
    server_version = "KeyLinker/1.0"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _headers(self) -> list[tuple[str, str]]:
        return [
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("Content-Security-Policy",
             "default-src 'self'; img-src 'self' data:; style-src 'self'; "
             "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"),
            ("Permissions-Policy", "geolocation=(), microphone=(), camera=()"),
        ]

    def _send(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        try:
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            for name, value in self._headers():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, message: str, status: int = 400) -> None:
        code, headers, body = _json_bytes({"error": message}, status)
        self._send(code, headers, body)

    def _attachment(self, filename: str, content_type: str,
                    payload: bytes | str) -> None:
        body = payload.encode("utf-8") if isinstance(payload, str) else payload
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename) or "download.csv"
        self._send(200, [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Content-Disposition", f'attachment; filename="{safe_name}"'),
            ("Cache-Control", "no-store"),
        ], body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if path.startswith("/api/"):
            self._api_get(path, query)
            return
        self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if not path.startswith("/api/"):
            self._error("不支持的方法", 405)
            return
        try:
            payload = self._read_json()
        except service.ApiError as exc:
            self._error(str(exc), exc.status)
            return
        self._api_post(path, payload)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/versions/([a-z0-9-]{1,64})", unquote(parsed.path))
        if not match:
            self._error("接口不存在", 404)
            return
        try:
            deleted = store.delete_version(match.group(1), ACTOR)
        except Exception as exc:  # pragma: no cover - storage failure
            self.log_message("storage error: %s", exc)
            self._error("存储错误", 500)
            return
        if not deleted:
            self._error("版本不存在", 404)
            return
        code, headers, body = _json_bytes({"deleted": match.group(1)})
        self._send(code, headers, body)

    def _read_json(self) -> dict[str, Any]:
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise service.ApiError("请求必须是 application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise service.ApiError("Content-Length 无效")
        if length <= 0:
            raise service.ApiError("请求体为空")
        if length > MAX_BODY:
            raise service.ApiError("请求体超过 12MB 上限", 413)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise service.ApiError("JSON 解析失败")
        if not isinstance(payload, dict):
            raise service.ApiError("请求体必须是 JSON 对象")
        return payload

    # ------------------------------------------------------------------
    # Static files
    # ------------------------------------------------------------------

    def _static(self, path: str) -> None:
        if path in STATIC_ROUTES:
            file = STATIC_DIR / STATIC_ROUTES[path]
            cache = "no-cache"
        elif path.startswith("/static/"):
            relative = path[len("/static/"):]
            if not relative or "/" in relative or "\\" in relative \
                    or relative.startswith("."):
                self._error("资源不存在", 404)
                return
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", relative):
                self._error("资源不存在", 404)
                return
            file = STATIC_DIR / relative
            cache = "public, max-age=300"
        else:
            self._error("页面不存在", 404)
            return
        try:
            resolved = file.resolve()
            if STATIC_DIR.resolve() not in resolved.parents:
                self._error("资源不存在", 404)
                return
            data = resolved.read_bytes()
        except (OSError, ValueError):
            self._error("资源不存在", 404)
            return
        mime, _ = mimetypes.guess_type(str(resolved))
        ctype = mime or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, [("Content-Type", ctype),
                         ("Content-Length", str(len(data))),
                         ("Cache-Control", cache)], data)

    # ------------------------------------------------------------------
    # GET API
    # ------------------------------------------------------------------

    def _api_get(self, path: str, query: dict[str, list[str]]) -> None:
        try:
            if path == "/api/meta":
                code, headers, body = _json_bytes(service.meta())
            elif path == "/api/versions":
                code, headers, body = _json_bytes(
                    {"versions": store.list_versions()})
            elif path == "/api/audit":
                limit = 100
                if query.get("limit"):
                    limit = max(1, min(500, int(query["limit"][0])))
                code, headers, body = _json_bytes(
                    {"audit": store.list_audit(limit)})
            elif path == "/api/runs":
                code, headers, body = _json_bytes(
                    {"runs": store.list_runs()})
            elif path.startswith("/api/versions/"):
                version_id = path[len("/api/versions/"):]
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", version_id):
                    raise service.ApiError("版本 id 无效")
                version = service.get_version(version_id)
                code, headers, body = _json_bytes({"version": version})
            elif path.startswith("/api/runs/"):
                run_id = path[len("/api/runs/"):]
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
                    raise service.ApiError("运行 id 无效")
                run = store.get_run(run_id)
                if not run:
                    raise service.ApiError("运行记录不存在", 404)
                code, headers, body = _json_bytes({"run": run})
            elif path.startswith("/api/samples/"):
                self._sample_download(path)
                return
            elif path == "/api/health":
                code, headers, body = _json_bytes(
                    {"ok": True, "version": API_VERSION})
            else:
                raise service.ApiError("接口不存在", 404)
            self._send(code, headers, body)
        except service.ApiError as exc:
            self._error(str(exc), exc.status)
        except (ValueError, KeyError) as exc:
            self._error(f"请求无效: {exc}")
        except Exception:
            self.log_message("GET %s failed:\n%s", path, traceback.format_exc())
            self._error("服务器内部错误", 500)

    def _sample_download(self, path: str) -> None:
        side = path[len("/api/samples/"):].rsplit(".", 1)[0]
        if side not in S.SAMPLES:
            raise service.ApiError("样本不存在", 404)
        filename, raw = S.sample_bytes(side)
        self._send(200, [
            ("Content-Type", "text/csv; charset=%s" % S.SAMPLES[side]["encoding"]),
            ("Content-Length", str(len(raw))),
            ("Content-Disposition", f'attachment; filename="{filename}"'),
            ("Cache-Control", "no-store"),
        ], raw)

    # ------------------------------------------------------------------
    # POST API
    # ------------------------------------------------------------------

    def _api_post(self, path: str, payload: dict[str, Any]) -> None:
        try:
            if path == "/api/import":
                result = service.import_payload(payload)
                code, headers, body = _json_bytes(result)
            elif path == "/api/sample/load":
                side = str(payload.get("side") or "")
                if side not in S.SAMPLES:
                    raise service.ApiError("样本不存在", 404)
                encoding = str(payload.get("encoding")
                               or S.SAMPLES[side]["encoding"])
                info = S.sample_text(side, encoding)
                sheet = service.parse_table(
                    str(info["text"]), str(info["filename"]), encoding)
                result = {
                    "side_data": service.sheet_to_side(
                        side, str(info["filename"]), encoding, sheet),
                    "replacement_chars": info["replacement_chars"],
                    "row_count": len(sheet.rows),
                    "column_count": len(sheet.columns),
                    "encoding": encoding,
                    "label": info["label"],
                }
                code, headers, body = _json_bytes(result)
            elif path == "/api/rules/preview":
                result = service.preview_rules(payload)
                code, headers, body = _json_bytes(result)
            elif path == "/api/preview":
                analysis = service.run_analysis(payload)
                analysis.pop("matches", None)
                analysis.pop("ambiguous_groups", None)
                analysis.pop("duplicate_groups", None)
                analysis.pop("unmatched", None)
                analysis.pop("excluded", None)
                analysis.pop("blocked_rows", None)
                code, headers, body = _json_bytes(analysis)
            elif path == "/api/analyze":
                analysis = service.analyze_with_baseline(payload)
                code, headers, body = _json_bytes({"analysis": analysis})
            elif path == "/api/versions":
                version = service.save_version(payload, ACTOR)
                code, headers, body = _json_bytes({"version": version}, 201)
            elif path == "/api/runs":
                result = service.save_run(payload, ACTOR)
                code, headers, body = _json_bytes(result, 201)
            elif path == "/api/export/csv":
                analysis = service.analyze_with_baseline(payload)
                version = None
                version_id = payload.get("version_id")
                if version_id:
                    version = service.get_version(str(version_id))
                filename, text = service.build_export(analysis, version)
                self._attachment(filename, "text/csv; charset=utf-8", text)
                return
            elif path == "/api/export/rules":
                analysis = service.run_analysis(payload)
                version = None
                version_id = payload.get("version_id")
                if version_id:
                    version = service.get_version(str(version_id))
                filename, text = service.export_rules_json(analysis, version)
                self._attachment(filename, "application/json; charset=utf-8",
                                 text)
                return
            else:
                raise service.ApiError("接口不存在", 404)
            self._send(code, headers, body)
        except service.ApiError as exc:
            self._error(str(exc), exc.status)
        except (ValueError, KeyError, TypeError) as exc:
            self._error(f"请求无效: {exc}")
        except Exception:
            self.log_message("POST %s failed:\n%s", path, traceback.format_exc())
            self._error("服务器内部错误", 500)


def create_app(db_path: Path | None = None) -> ThreadingHTTPServer:
    store.init_db(db_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    host = os.environ.get("LINKER_HOST", "127.0.0.1")
    port = int(os.environ.get("LINKER_PORT", "8765"))
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if "--host" in argv:
        host = argv[argv.index("--host") + 1]
    if not (0 <= port <= 65535):
        print("端口无效", file=sys.stderr)
        return 2
    store.init_db()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"KeyLinker running at http://{host}:{port}/", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
