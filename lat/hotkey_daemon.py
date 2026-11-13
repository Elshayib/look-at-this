"""In-process single-instance OS global hotkey manager for Look At This.

Runs automatically inside ``hermes serve`` (started lazily by ``plugin_api.py``,
never during ``hermes plugins validate`` or ``doctor``). Registers two system-wide
chords on Windows:
  1. ``window_hotkey`` (default ``ctrl+alt+l``) -> instant foreground window grab + focus Hermes
  2. ``snip_hotkey``   (default ``ctrl+alt+s``) -> fullscreen interactive window/region snip + focus Hermes

Supports live chord reloading without restarting Hermes via ``PostThreadMessageW``.
Never displays blocking Win32 ``MessageBoxW`` modals; all statuses and errors
report cleanly over Hermes's live plugin event bus.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from typing import Any

if sys.platform == "win32":
    import ctypes.wintypes as wt

from . import capture
from .models import CaptureCancelledError, LookAtError, load_live_settings
from .store import STORE, emit_plugin_event, get_data_dir

logger = logging.getLogger(__name__)


def configure_hotkey_ctypes(windll: Any | None = None) -> None:
    """Declare explicit 64-bit-safe ctypes argtypes/restype for hotkey Win32 calls."""
    import ctypes.wintypes as wintypes

    dll_root = windll if windll is not None else getattr(ctypes, "windll", None)
    if dll_root is None:
        return

    user32 = dll_root.user32
    kernel32 = dll_root.kernel32

    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.RegisterHotKey.argtypes = [
        wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT,
    ]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
    ]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
    ]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t


if sys.platform == "win32":
    configure_hotkey_ctypes()

WM_HOTKEY = 0x0312
WM_APP_RELOAD = 0x8001
WM_APP_STOP = 0x8002
MOD_NOREPEAT = 0x4000

ID_WINDOW_HOTKEY = 101
ID_SNIP_HOTKEY = 102

_MOD_BITS: dict[str, int] = {
    "alt": 0x0001,
    "option": 0x0001,
    "ctrl": 0x0002,
    "control": 0x0002,
    "shift": 0x0004,
    "win": 0x0008,
    "meta": 0x0008,
    "cmd": 0x0008,
    "mod": 0x0002,
}

_NAMED_VKS: dict[str, int] = {
    "space": 0x20,
    "enter": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "grave": 0xC0,
    "`": 0xC0,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
    "\\": 0xDC,
}


def parse_hotkey_chord(chord: str) -> tuple[int, int] | None:
    """Parse ``'ctrl+alt+l'`` into ``(win32_mod_mask, virtual_key_code)``, or None if blank."""
    clean = str(chord or "").strip().lower()
    if not clean or clean in {"none", "off", "disabled"}:
        return None
    parts = [p.strip() for p in clean.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError(f"Hotkey {chord!r} must include at least one modifier (e.g. ctrl+alt+l)")

    mods = 0
    for mod_token in parts[:-1]:
        bit = _MOD_BITS.get(mod_token)
        if bit is None:
            raise ValueError(f"Unknown modifier {mod_token!r} in {chord!r}")
        mods |= bit

    key = parts[-1]
    if key in _NAMED_VKS:
        vk = _NAMED_VKS[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError(f"Unsupported hotkey key {key!r} in {chord!r}")
    return mods, vk


class GlobalHotkeyManager:
    """Manages a single background Win32 message-only thread for OS-wide hotkeys."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._win_tid: int = 0
        self._lock_file: Any = None
        self._lock_path: Any = None
        self._busy = threading.Lock()
        self._state: dict[str, Any] = {
            "supported": sys.platform == "win32",
            "running": False,
            "window_hotkey": "",
            "window_registered": False,
            "snip_hotkey": "",
            "snip_registered": False,
            "warnings": [],
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _release_lock_handle(self, fh: Any) -> None:
        if fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass

    def _acquire_process_lock(self) -> bool:
        path = get_data_dir() / "hotkey.lock"
        if self._lock_file is not None:
            if self._lock_path == path:
                return True
            self._release_lock_handle(self._lock_file)
            self._lock_file = None
            self._lock_path = None
        try:
            fh = open(path, "a+b")
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"1")
                fh.flush()
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_file = fh
            self._lock_path = path
            return True
        except OSError:
            try:
                fh.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            return False

    def ensure_started(self) -> dict[str, Any]:
        if sys.platform != "win32":
            return self.status()
        target_lock = get_data_dir() / "hotkey.lock"
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and self._lock_path != target_lock:
                # HERMES_HOME changed (e.g. isolated test run) -> rebind cleanly
                need_rebind = True
            else:
                need_rebind = False
        if need_rebind:
            self.stop()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            if not self._acquire_process_lock():
                self._state["warnings"] = ["Another Hermes instance already owns the system-wide hotkeys."]
                return self.status()

            ready_evt = threading.Event()
            t = threading.Thread(target=self._loop, args=(ready_evt,), daemon=True, name="lat-global-hotkeys")
            self._thread = t
            t.start()
        ready_evt.wait(timeout=2.0)
        capture.warmup()
        return self.status()

    def reload(self) -> dict[str, Any]:
        if sys.platform != "win32":
            return self.status()
        with self._lock:
            tid = self._win_tid
            alive = self._thread is not None and self._thread.is_alive()
        if not alive:
            return self.ensure_started()
        if tid:
            try:
                configure_hotkey_ctypes()
                ctypes.windll.user32.PostThreadMessageW(wt.DWORD(tid), WM_APP_RELOAD, 0, 0)
                time.sleep(0.05)
            except Exception:
                pass
        return self.status()

    def stop(self) -> None:
        with self._lock:
            tid = self._win_tid
            thread = self._thread
            fh = self._lock_file
            self._lock_file = None
            self._lock_path = None
            self._thread = None
        if sys.platform == "win32" and tid:
            try:
                configure_hotkey_ctypes()
                ctypes.windll.user32.PostThreadMessageW(wt.DWORD(tid), WM_APP_STOP, 0, 0)
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._release_lock_handle(fh)

    def _apply_registrations(self, user32: Any) -> None:
        user32.UnregisterHotKey(None, ID_WINDOW_HOTKEY)
        user32.UnregisterHotKey(None, ID_SNIP_HOTKEY)

        settings = load_live_settings()
        warnings: list[str] = []
        win_ok = False
        snip_ok = False

        for hk_id, chord, label in (
            (ID_WINDOW_HOTKEY, settings.window_hotkey, "window_hotkey"),
            (ID_SNIP_HOTKEY, settings.snip_hotkey, "snip_hotkey"),
        ):
            try:
                parsed = parse_hotkey_chord(chord)
            except ValueError as exc:
                warnings.append(str(exc))
                continue
            if parsed is None:
                continue
            mods, vk = parsed
            ok = bool(user32.RegisterHotKey(None, hk_id, mods | MOD_NOREPEAT, vk))
            if not ok:
                warnings.append(f"Shortcut {chord!r} is already in use by another app.")
            elif label == "window_hotkey":
                win_ok = True
            else:
                snip_ok = True

        with self._lock:
            self._state.update({
                "running": True,
                "window_hotkey": settings.window_hotkey,
                "window_registered": win_ok,
                "snip_hotkey": settings.snip_hotkey,
                "snip_registered": snip_ok,
                "warnings": warnings,
            })

    def _handle_hotkey(self, hk_id: int) -> None:
        if not self._busy.acquire(blocking=False):
            return
        try:
            settings = load_live_settings()
            hk_target = settings.hotkey_target
            if hk_target == "current_session":
                session_id = STORE.get_focused_session()
                target_mode = "session" if session_id else "any"
            else:
                session_id = ""
                target_mode = hk_target

            if hk_id == ID_WINDOW_HOTKEY:
                raw = capture.capture_window(
                    settings,
                    include_screenshot=True,
                    include_tree=settings.include_full_tree_by_default,
                )
            else:
                raw = capture.capture_interactive_snip(
                    settings,
                    include_tree=settings.include_full_tree_by_default,
                )

            pkt = STORE.build_and_store(
                raw, settings, session_id=session_id, target_mode=target_mode
            )
            evt_meta = STORE.record_hotkey_capture(
                pkt,
                hk_target,
                focus_composer=settings.focus_hermes_on_hotkey,
            )
            if settings.hotkey_hud_feedback:
                dest_lbl = (
                    "opening new session"
                    if hk_target == "new_session"
                    else "choose session in Hermes"
                    if hk_target == "ask"
                    else "attached to chat"
                )
                capture.show_capture_feedback(f"Captured {pkt.app_display} — {dest_lbl}")
            if settings.focus_hermes_on_hotkey:
                capture.focus_hermes()
            from .render import format_public_packet

            emit_plugin_event(
                "hotkey.captured",
                {
                    **evt_meta,
                    "pending": format_public_packet(pkt, include_preview=True),
                },
            )
        except CaptureCancelledError:
            pass
        except LookAtError as exc:
            logger.info("look-at-this hotkey capture refused: %s", exc)
            emit_plugin_event("hotkey.error", exc.to_dict())
        except Exception as exc:
            logger.exception("look-at-this hotkey failure")
            emit_plugin_event("hotkey.error", {"ok": False, "code": "internal_error", "error": str(exc)})
        finally:
            self._busy.release()

    def _loop(self, ready_evt: threading.Event) -> None:
        configure_hotkey_ctypes()
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        msg = wt.MSG()
        # Force message queue creation on this thread before PostThreadMessageW can target it
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        with self._lock:
            self._win_tid = int(kernel32.GetCurrentThreadId())

        self._apply_registrations(user32)
        ready_evt.set()

        try:
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got <= 0:
                    break
                if msg.message == WM_APP_STOP:
                    break
                if msg.message == WM_APP_RELOAD:
                    self._apply_registrations(user32)
                    continue
                if msg.message == WM_HOTKEY:
                    hk_id = int(msg.wParam)
                    # For window capture, run right on the hotkey thread so Win32
                    # SetForegroundWindow retains the OS WM_HOTKEY foreground token!
                    self._handle_hotkey(hk_id)
        finally:
            user32.UnregisterHotKey(None, ID_WINDOW_HOTKEY)
            user32.UnregisterHotKey(None, ID_SNIP_HOTKEY)
            with self._lock:
                self._win_tid = 0
                self._state["running"] = False
                self._state["window_registered"] = False
                self._state["snip_registered"] = False


def _shared_hotkey_manager() -> GlobalHotkeyManager:
    key = "_look_at_this_shared_hotkey_manager"
    existing = getattr(sys, key, None)
    if existing is not None and hasattr(existing, "ensure_started") and hasattr(existing, "stop"):
        # Adopt if same class layout; otherwise stop the stale one cleanly before replacing
        if type(existing).__name__ == "GlobalHotkeyManager" and hasattr(existing, "_release_lock_handle"):
            return existing  # type: ignore[return-value]
        try:
            existing.stop()
        except Exception:
            pass
    mgr = GlobalHotkeyManager()
    setattr(sys, key, mgr)
    return mgr


HOTKEYS = _shared_hotkey_manager()
