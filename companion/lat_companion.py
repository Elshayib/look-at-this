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
import ctypes
import ctypes.wintypes
import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
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


def load_settings():
    """Read plugins.entries.look-at-this.settings; fall back to schema defaults."""
    data: dict = {}
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        entry = (((cfg.get("plugins") or {}).get("entries") or {}).get("look-at-this") or {})
        raw = entry.get("settings") or {}
        if isinstance(raw, dict):
            data = raw
    except Exception:
        pass
    return lat_core.Settings.from_getter(lambda key, default=None: data.get(key, default)), data


# ─────────────────────────────────────────────────────────────────────────────
# Global hotkey (Windows): capture the app you are in, then open Quick Entry
# ─────────────────────────────────────────────────────────────────────────────

_MOD_BITS = {"ctrl": 0x0002, "control": 0x0002, "alt": 0x0001, "shift": 0x0004,
             "win": 0x0008, "meta": 0x0008}
_MOD_VKS = ((0x0002, 0x11), (0x0004, 0x10), (0x0001, 0x12), (0x0008, 0x5B))
_VK_NAMES = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B}


def parse_chord(chord: str) -> tuple[int, int]:
    parts = [p.strip().lower() for p in str(chord).split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    key = parts[-1]
    mods = 0
    for part in parts[:-1]:
        if part not in _MOD_BITS:
            raise ValueError(f"unknown modifier {part!r}")
        mods |= _MOD_BITS[part]
    if key in _VK_NAMES:
        vk = _VK_NAMES[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError(f"unsupported key {key!r}")
    return mods, vk


def send_chord(chord: str) -> None:
    """Synthesize a chord OS-wide (used to open Hermes Quick Entry)."""
    mods, vk = parse_chord(chord)
    user32 = ctypes.windll.user32
    held = [vk_mod for bit, vk_mod in _MOD_VKS if mods & bit]
    for vk_mod in held:
        user32.keybd_event(vk_mod, 0, 0, 0)
    time.sleep(0.02)
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)
    time.sleep(0.02)
    for vk_mod in reversed(held):
        user32.keybd_event(vk_mod, 0, 2, 0)


def run_hotkey(settings: lat_core.Settings, hotkey: str, entry_chord: str) -> int:
    if sys.platform != "win32":
        print("Global hotkey mode is Windows-only today.")
        return 2
    try:
        mods, vk = parse_chord(hotkey)
        parse_chord(entry_chord)
    except ValueError as exc:
        print(f"Bad hotkey: {exc}")
        return 2

    user32 = ctypes.windll.user32
    hotkey_id = 1
    if not user32.RegisterHotKey(None, hotkey_id, mods | 0x4000, vk):  # MOD_NOREPEAT
        user32.MessageBoxW(None, f"{hotkey} is already taken by another application.",
                           "Look At This", 0x10)
        return 1

    print(f"Look At This: press {hotkey} in any app to ask about that window.")
    print(f"Quick Entry chord: {entry_chord} (must match Hermes' setting)")

    msg = ctypes.wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message != 0x0312:  # WM_HOTKEY
                continue
            try:
                cap = lat_core.capture(settings)
                pkt = lat_core.make_packet(cap, settings, "")
                lat_core.spool_packet(pkt)
            except lat_core.LookAtError as exc:
                user32.MessageBoxW(None, f"{exc}", "Look At This", 0x10)
                continue
            except Exception as exc:  # never kill the hotkey loop
                user32.MessageBoxW(None, f"{type(exc).__name__}: {exc}", "Look At This", 0x10)
                continue
            send_chord(entry_chord)
    finally:
        user32.UnregisterHotKey(None, hotkey_id)
    return 0


def _startup_lnk() -> Path:
    return (Path(os.environ.get("APPDATA", str(Path.home())))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            / "Look At This Companion.lnk")


def install_autostart() -> int:
    if sys.platform != "win32":
        print("Autostart is Windows-only today.")
        return 2
    script = Path(__file__).resolve()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    lnk = _startup_lnk()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    ps1 = Path(tempfile.gettempdir()) / "lat_autostart.ps1"
    ps1.write_text(
        "$ws = New-Object -ComObject WScript.Shell\n"
        f"$s = $ws.CreateShortcut('{lnk}')\n"
        f"$s.TargetPath = '{exe}'\n"
        f"$s.Arguments = '\"{script}\" --hotkey'\n"
        f"$s.WorkingDirectory = '{script.parent}'\n"
        "$s.Save()\n",
        encoding="utf-8",
    )
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-File", str(ps1)], capture_output=True, text=True)
    ps1.unlink(missing_ok=True)
    if proc.returncode != 0:
        print(proc.stderr.strip() or "Could not create the startup shortcut.")
        return 1
    print(f"Installed: {lnk}")
    print("It starts at your next login. Run it once now to use it immediately.")
    return 0


def remove_autostart() -> int:
    lnk = _startup_lnk()
    if lnk.exists():
        lnk.unlink()
        print(f"Removed: {lnk}")
    else:
        print("No startup shortcut found.")
    return 0


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
    parser.add_argument("--install-autostart", action="store_true")
    parser.add_argument("--remove-autostart", action="store_true")
    args = parser.parse_args(argv)

    if args.remove_autostart:
        return remove_autostart()
    if args.install_autostart:
        return install_autostart()

    settings, raw = load_settings()
    token = load_or_create_token(args.token, args.token_file)
    if args.print_token:
        print(token)
        return 0

    if args.hotkey:
        hotkey = args.hotkey_chord or str(raw.get("global_hotkey") or "ctrl+alt+l")
        entry = args.entry_chord or str(raw.get("quick_entry_hotkey") or "ctrl+shift+space")
        if not args.no_serve:
            Handler.token = token
            server = ThreadingHTTPServer((args.host, args.port), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            print(f"HTTP capture on http://{args.host}:{args.port}")
        try:
            return run_hotkey(settings, hotkey, entry)
        except KeyboardInterrupt:
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
