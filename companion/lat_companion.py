#!/usr/bin/env python3
"""Look At This — local capture companion.

Runs on the machine whose screen you want to capture. A Hermes gateway that is
NOT on this machine (Docker, an SSH terminal backend, a home server) can call
``POST /capture`` instead of driving a local driver; the Desktop's global
hotkey can also live here (``--hotkey``), capturing the window in front of you
and then opening Quick Entry.

What it does: resolves the driver exactly like the plugin, captures the
foreground window (or the window beneath the last non-Hermes window), and
returns the packet fields plus the PNG as base64.

Hotkey captures are held in this process's memory and served over the same
authenticated port:

    GET   /pending?session_id=...&preview=1   the packet, without consuming it
    PATCH /pending                            change which fields it exposes
    POST  /take                               consume it (PNG as base64)
    POST  /clear                              discard it

Only a metadata marker (packet id, session id, expiry) is written to
``<HERMES_HOME>/plugin-data/look-at-this/pending.json`` and the PNG to
``shots/``; the packet text (title, URL, selection, tree) never touches disk.
The PNG is deleted when the packet expires, is cleared, or a newer capture
replaces it. This process never dials out.

Auth: a bearer token, generated on first run into
``<HERMES_HOME>/plugin-data/look-at-this/companion.token`` (mode 0600) unless
``--token`` / ``--token-file`` is given. Bind to 127.0.0.1 by default; expose it
on a LAN/Tailscale interface only deliberately (``--host 0.0.0.0``) and only
over a trusted network — the token is the only gate.

Usage:
    python companion/lat_companion.py                 # 127.0.0.1:7391
    python companion/lat_companion.py --host 0.0.0.0  # reachable on the LAN
    python companion/lat_companion.py --print-token   # show the token and exit
    python companion/lat_companion.py --hotkey        # global hotkey + HTTP
    python companion/lat_companion.py --install-autostart
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hmac
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import lat_core  # noqa: E402

if __package__:  # imported as part of the package, e.g. `-m companion.lat_companion`
    from . import win_hotkey  # noqa: E402
else:  # run as a script: `python companion/lat_companion.py`
    from companion import win_hotkey  # noqa: E402

MAX_BODY = 16 * 1024
DEFAULT_PORT = 7391


def token_path() -> Path:
    return lat_core.data_dir() / "companion.token"


def load_or_create_token(explicit: str = "", explicit_file: str = "") -> str:
    value = str(explicit or "").strip()
    if value:
        return value
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


# ─────────────────────────────────────────────────────────────────────────────
# Pending box: the one hotkey packet this process is holding, never on disk
# ─────────────────────────────────────────────────────────────────────────────


def _unlink_png(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


class PendingBox:
    """One packet at a time, TTL-bound, guarded by a lock.

    A packet captured from a fresh chat has ``session_id == ""`` and may be
    claimed by any request. A packet bound to a real session is claimable only
    by that session — a blank request must never see it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._packet: lat_core.Packet | None = None

    @staticmethod
    def _matches(pkt: lat_core.Packet, session_id: str) -> bool:
        return pkt.session_id == "" or pkt.session_id == session_id

    @staticmethod
    def _expired(pkt: lat_core.Packet) -> bool:
        return pkt.expires_at <= time.time()

    def put(self, pkt: lat_core.Packet) -> lat_core.Packet:
        """Replace the held packet; a superseded PNG is deleted."""
        self.sweep()
        with self._lock:
            old = self._packet
            self._packet = pkt
        if old is not None and old.screenshot_path != pkt.screenshot_path:
            _unlink_png(old.screenshot_path)
        return pkt

    def get(self, session_id: str) -> lat_core.Packet | None:
        with self._lock:
            pkt = self._packet
            if pkt is None or not self._matches(pkt, session_id):
                return None
            if not self._expired(pkt):
                return pkt
            self._packet = None
        lat_core.clear_pending_marker(pkt.id)
        _unlink_png(pkt.screenshot_path)
        return None

    def take(self, session_id: str) -> lat_core.Packet | None:
        """Remove and return the packet on a match. The PNG survives to TTL."""
        stale_path = None
        result = None
        expired = False
        with self._lock:
            pkt = self._packet
            if pkt is None or not self._matches(pkt, session_id):
                return None
            self._packet = None
            if self._expired(pkt):
                stale_path = pkt.screenshot_path
                expired = True
            else:
                result = pkt
        if expired:
            lat_core.clear_pending_marker(pkt.id)
        _unlink_png(stale_path)
        return result

    def update_fields(self, session_id: str, fields: list[str]) -> lat_core.Packet | None:
        stale_path = None
        result = None
        expired = False
        with self._lock:
            pkt = self._packet
            if pkt is None or not self._matches(pkt, session_id):
                return None
            if self._expired(pkt):
                self._packet = None
                stale_path = pkt.screenshot_path
                expired = True
            else:
                pkt.fields = [f for f in fields if f in lat_core.ALL_FIELDS]
                if "screenshot" not in pkt.fields and pkt.screenshot_path:
                    stale_path = pkt.screenshot_path
                    pkt.screenshot_path = None
                result = pkt
        if expired:
            lat_core.clear_pending_marker(pkt.id)
        _unlink_png(stale_path)
        return result

    def clear(self, session_id: str, expect_id: str | None = None) -> bool:
        with self._lock:
            pkt = self._packet
            if pkt is None or not self._matches(pkt, session_id):
                return False
            if expect_id and pkt.id != expect_id:
                return False
            self._packet = None
        _unlink_png(pkt.screenshot_path)
        return True

    def sweep(self) -> None:
        with self._lock:
            pkt = self._packet
            if pkt is None or not self._expired(pkt):
                return
            self._packet = None
        lat_core.clear_pending_marker(pkt.id)
        _unlink_png(pkt.screenshot_path)

    def is_empty(self) -> bool:
        with self._lock:
            return self._packet is None


BOX = PendingBox()


def _read_screenshot_b64(path: str | None) -> str | None:
    if not path:
        return None
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if not raw or len(raw) > lat_core.MAX_SCREENSHOT_BYTES:
        return None
    return base64.b64encode(raw).decode("ascii")


# ─────────────────────────────────────────────────────────────────────────────
# HTTP: capture, plus the pending packet held in this process
# ─────────────────────────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    server_version = "LookAtThisCompanion/0.1"
    token = ""
    settings = lat_core.Settings()
    lock = threading.Lock()

    def log_message(self, fmt: str, *args):  # keep the console quiet
        if os.environ.get("LAT_COMPANION_DEBUG"):
            super().log_message(fmt, *args)

    def _target(self) -> tuple[str, dict[str, list[str]]]:
        parts = urlsplit(self.path)
        return (parts.path.rstrip("/") or "/"), parse_qs(parts.query, keep_blank_values=True)

    def _auth(self) -> bool:
        if not self.token:
            return False
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(
            header[len(prefix):].strip().encode("utf-8"), self.token.encode("utf-8")
        )

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._json(400, {"ok": False, "error": "invalid body"})
            return None
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"ok": False, "error": "invalid json"})
            return None
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "error": "invalid body"})
            return None
        return payload

    # -- GET /health, GET /pending -----------------------------------------

    def do_GET(self):  # noqa: N802
        path, query = self._target()
        if path not in ("/health", "/pending"):
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._auth():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        if path == "/health":
            self._json(200, {
                "ok": True,
                "version": "0.1.0",
                "driver": lat_core.resolve_driver(),
                "platform": sys.platform,
            })
            return
        session_id = (query.get("session_id") or [""])[0].strip()
        preview = (query.get("preview") or ["0"])[0].strip().lower() in ("1", "true", "yes", "on")
        pkt = BOX.get(session_id)
        if pkt is None:
            self._json(200, {"ok": True, "pending": None, "packet": None})
            return
        self._json(200, {
            "ok": True,
            "pending": pkt.public(include_preview=preview),
            "packet": pkt.to_dict(),
        })

    # -- PATCH /pending ------------------------------------------------------

    def do_PATCH(self):  # noqa: N802
        path, _query = self._target()
        if path != "/pending":
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._auth():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        payload = self._read_json()
        if payload is None:
            return
        raw_fields = payload.get("fields")
        fields = ([str(f) for f in raw_fields if str(f) in lat_core.ALL_FIELDS]
                  if isinstance(raw_fields, list) else [])
        if not fields:
            self._json(400, {
                "ok": False,
                "error": "fields must name at least one of " + ", ".join(lat_core.ALL_FIELDS),
            })
            return
        session_id = str(payload.get("session_id") or "").strip()
        pkt = BOX.update_fields(session_id, fields)
        if pkt is None:
            self._json(200, {"ok": True, "pending": None, "packet": None})
            return
        self._json(200, {"ok": True, "pending": pkt.public(), "packet": pkt.to_dict()})

    # -- POST /capture, /take, /clear ---------------------------------------

    def do_POST(self):  # noqa: N802
        path, _query = self._target()
        if path == "/capture":
            self._capture()
            return
        if path == "/active-session":
            if not self._auth():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            payload = self._read_json()
            if payload is None:
                return
            lat_core.write_active_session(str(payload.get("session_id") or "").strip())
            self._json(200, {"ok": True})
            return
        if path not in ("/take", "/clear"):
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._auth():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        payload = self._read_json()
        if payload is None:
            return
        session_id = str(payload.get("session_id") or "").strip()
        if path == "/take":
            pkt = BOX.take(session_id)
            if pkt is None:
                self._json(200, {"ok": True, "packet": None, "screenshot_b64": None})
                return
            shot_b64 = _read_screenshot_b64(pkt.screenshot_path)
            lat_core.clear_pending_marker(pkt.id)
            self._json(200, {
                "ok": True,
                "packet": pkt.to_dict(),
                "screenshot_b64": shot_b64,
            })
            return
        pkt = BOX.get(session_id)
        cleared = BOX.clear(session_id, expect_id=pkt.id) if pkt else False
        if cleared:
            lat_core.clear_pending_marker(pkt.id)
        self._json(200, {"ok": True, "cleared": cleared})

    def _capture(self) -> None:
        if not self._auth():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        payload = self._read_json()
        if payload is None:
            return

        hint = str(payload.get("hint") or "").strip()
        raw_fields = payload.get("fields")
        fields = ([str(f) for f in raw_fields if str(f) in lat_core.ALL_FIELDS]
                  if isinstance(raw_fields, list) else [])
        # Force the local driver: when the configured backend is "companion",
        # calling it here would HTTP back into this same server (recursion).
        local = dataclasses.replace(self.settings, capture_backend="local")
        try:
            with self.lock:
                cap = lat_core.capture(local, hint=hint)
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


# ─────────────────────────────────────────────────────────────────────────────
# Global hotkey (Windows): capture the app you are in, then open Quick Entry
# ─────────────────────────────────────────────────────────────────────────────


def capture_for_hotkey() -> bool:
    """The hotkey callback: capture locally, hold the packet, write metadata only.

    Returns False when the capture was refused/failed, so ``run_hotkey`` does not
    synthesize the Quick Entry chord into whatever dialog is now in front.
    """
    settings = lat_core.load_settings()[0]
    try:
        local = dataclasses.replace(settings, capture_backend="local")
        cap = lat_core.capture(local)
        pkt = lat_core.make_packet(cap, settings, lat_core.read_active_session())
        BOX.put(pkt)
        lat_core.write_pending_marker(pkt)
        return True
    except lat_core.LookAtError as exc:
        win_hotkey.message_box(str(exc))
        return False
    except Exception as exc:  # never kill the hotkey loop
        win_hotkey.message_box(f"{type(exc).__name__}: {exc}")
        return False


def _sweeper(ttl_seconds: int) -> None:
    """Drop expired packets/markers every 30 s and reap orphaned PNGs."""
    while True:
        time.sleep(30)
        try:
            BOX.sweep()
        except Exception:
            pass
        try:
            lat_core.STORE.sweep_files(ttl_seconds)
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Look At This capture companion")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default="")
    parser.add_argument("--token-file", default="")
    parser.add_argument("--print-token", action="store_true")
    parser.add_argument("--hotkey", action="store_true",
                        help="Run the global hotkey: capture the app you are in and open Quick Entry")
    parser.add_argument("--hotkey-chord", default="",
                        help="Global chord (default: plugins.entries.look-at-this.settings.global_hotkey)")
    parser.add_argument("--entry-chord", default="",
                        help="Chord that opens Hermes Quick Entry (default: quick_entry_hotkey setting)")
    parser.add_argument("--no-serve", action="store_true", help="Hotkey only; do not open the HTTP port")
    parser.add_argument("--no-open", action="store_true",
                        help="Queue the packet without synthesizing the Quick Entry chord")
    parser.add_argument("--install-autostart", action="store_true")
    parser.add_argument("--remove-autostart", action="store_true")
    args = parser.parse_args(argv)

    if args.remove_autostart:
        return win_hotkey.remove_autostart()
    if args.install_autostart:
        return win_hotkey.install_autostart(Path(__file__).resolve())

    settings, _raw = lat_core.load_settings()
    threading.Thread(target=_sweeper, args=(settings.ttl_seconds,), daemon=True).start()
    token = load_or_create_token(args.token, args.token_file)
    if args.print_token:
        print(token)
        return 0

    Handler.settings = settings
    Handler.token = token

    if args.hotkey:
        hotkey = args.hotkey_chord or settings.global_hotkey
        entry = "" if args.no_open else (args.entry_chord or settings.quick_entry_hotkey)
        if not args.no_serve:
            server = ThreadingHTTPServer((args.host, args.port), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            print(f"HTTP capture on http://{args.host}:{args.port}")
        try:
            return win_hotkey.run_hotkey(hotkey, entry, capture_for_hotkey)
        except KeyboardInterrupt:
            return 0

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
