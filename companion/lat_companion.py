#!/usr/bin/env python3
"""Look At This — local capture companion.

Runs on the machine whose screen you want to capture and serves one endpoint,
``POST /capture``, so a Hermes gateway that is NOT on this machine (Docker, an
SSH terminal backend, a home server) can still capture *your* window. The
gateway calls in; this process never dials out.

What it does: resolves the driver exactly like the plugin, captures the
foreground window (or the window beneath the last non-Hermes window), and
returns the packet fields plus the PNG as base64. Nothing is stored here: no
history, no queue, no disk copy beyond the driver's temporary screenshot.

Auth: a bearer token, generated on first run into
``<HERMES_HOME>/plugin-data/look-at-this/companion.token`` (mode 0600) unless
``--token`` / ``--token-file`` is given. Bind to 127.0.0.1 by default; expose it
on a LAN/Tailscale interface only deliberately (``--host 0.0.0.0``) and only
over a trusted network — the token is the only gate.

Usage:
    python companion/lat_companion.py                 # 127.0.0.1:7391
    python companion/lat_companion.py --host 0.0.0.0  # reachable on the LAN
    python companion/lat_companion.py --print-token   # show the token and exit
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import lat_core  # noqa: E402

MAX_BODY = 16 * 1024
DEFAULT_PORT = 7391


def token_path() -> Path:
    return lat_core.data_dir() / "companion.token"


def load_or_create_token(explicit: str = "", explicit_file: str = "") -> str:
    if explicit:
        return explicit.strip()
    path = Path(explicit_file).expanduser() if explicit_file else token_path()
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "LookAtThisCompanion/0.1"
    token = ""
    settings = lat_core.Settings()
    lock = threading.Lock()

    def log_message(self, fmt: str, *args):  # keep the console quiet
        if os.environ.get("LAT_COMPANION_DEBUG"):
            super().log_message(fmt, *args)

    def _auth(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):].strip(), self.token)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") != "/health":
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._auth():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        self._json(200, {
            "ok": True,
            "version": "0.1.0",
            "driver": lat_core.resolve_driver(),
            "platform": sys.platform,
        })

    def do_POST(self):  # noqa: N802
        if self.path.rstrip("/") != "/capture":
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._auth():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._json(400, {"ok": False, "error": "invalid body"})
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"ok": False, "error": "invalid json"})
            return

        hint = str(payload.get("hint") or "").strip()
        fields = payload.get("fields")
        fields = [str(f) for f in fields if str(f) in lat_core.ALL_FIELDS] if isinstance(fields, list) else []
        try:
            with self.lock:
                cap = lat_core.capture(self.settings, hint=hint)
            shot_b64 = None
            if cap.screenshot_path and (not fields or "screenshot" in fields):
                raw = Path(cap.screenshot_path).read_bytes()
                shot_b64 = base64.b64encode(raw).decode("ascii")
                Path(cap.screenshot_path).unlink(missing_ok=True)
            self._json(200, {
                "ok": True,
                "packet": {
                    "app": cap.app,
                    "pid": cap.pid,
                    "title": cap.title,
                    "url": cap.url,
                    "url_source": cap.url_source,
                    "selection": cap.selection,
                    "selection_source": cap.selection_source,
                    "tree_markdown": cap.tree_markdown,
                    "element_count": cap.element_count,
                    "returned_count": cap.returned_count,
                    "truncated": cap.truncated,
                    "screenshot_b64": shot_b64,
                    "screenshot_width": cap.screenshot_width,
                    "screenshot_height": cap.screenshot_height,
                },
            })
        except lat_core.LookAtError as exc:
            self._json(400, exc.as_dict())
        except Exception as exc:
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Look At This capture companion")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default="")
    parser.add_argument("--token-file", default="")
    parser.add_argument("--print-token", action="store_true")
    args = parser.parse_args(argv)

    token = load_or_create_token(args.token, args.token_file)
    if args.print_token:
        print(token)
        return 0

    Handler.token = token
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Look At This companion on http://{args.host}:{args.port}")
    print(f"token file: {token_path()}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: bound to a non-loopback interface; the token is the only gate.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
