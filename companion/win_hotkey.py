"""Windows-only global hotkey and autostart helpers for the companion.

Split out of ``lat_companion.py`` so the HTTP half stays importable on every
platform: ``ctypes.windll`` and the Startup folder only exist on Windows.
Nothing here captures or stores anything; ``run_hotkey`` calls back into the
companion whenever the registered chord fires.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

try:  # ctypes.wintypes raises ValueError on non-Windows, ImportError nowhere
    import ctypes.wintypes as wintypes
except (ImportError, ValueError):  # pragma: no cover - non-Windows import
    wintypes = None  # type: ignore[assignment]

WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000

# One table: name -> (RegisterHotKey modifier bit, virtual-key code). Both the
# mask used for registration and the keys synthesized by send_chord come from
# here, so ctrl/alt/shift/win are never encoded twice.
_MODIFIERS: dict[str, tuple[int, int]] = {
    "ctrl": (0x0002, 0x11),      # VK_CONTROL
    "control": (0x0002, 0x11),
    "alt": (0x0001, 0x12),       # VK_MENU
    "shift": (0x0004, 0x10),     # VK_SHIFT
    "win": (0x0008, 0x5B),       # VK_LWIN
    "meta": (0x0008, 0x5B),
}

# Press ctrl/shift/alt before win so Alt does not open a menu mid-chord.
_PRESS_ORDER = ("ctrl", "shift", "alt", "win")

_VK_NAMES = {
    "space": 0x20,
    "enter": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
}


def parse_chord(chord: str) -> tuple[int, int]:
    """``"ctrl+alt+l"`` -> ``(MOD_CONTROL | MOD_ALT, VK_L)``."""
    parts = [part.strip().lower() for part in str(chord).split("+") if part.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    key = parts[-1]
    mods = 0
    for part in parts[:-1]:
        entry = _MODIFIERS.get(part)
        if entry is None:
            raise ValueError(f"unknown modifier {part!r}")
        mods |= entry[0]
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
    if sys.platform != "win32":
        raise RuntimeError("Synthetic keyboard chords are Windows-only today.")
    mods, vk = parse_chord(chord)
    user32 = ctypes.windll.user32
    held = [_MODIFIERS[name][1] for name in _PRESS_ORDER if mods & _MODIFIERS[name][0]]
    for vk_mod in held:
        user32.keybd_event(vk_mod, 0, 0, 0)
    time.sleep(0.02)
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)
    time.sleep(0.02)
    for vk_mod in reversed(held):
        user32.keybd_event(vk_mod, 0, 2, 0)


def message_box(text: str, title: str = "Look At This", error: bool = True) -> None:
    """A blocking MessageBox on Windows; stderr elsewhere. Never raises."""
    text = str(text)
    if sys.platform != "win32":
        print(f"{title}: {text}", file=sys.stderr)
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)
    except Exception:  # a missing desktop/session must not kill the loop
        print(f"{title}: {text}", file=sys.stderr)


def run_hotkey(hotkey: str, entry_chord: str, on_hotkey: Callable[[], None]) -> int:
    """Register *hotkey*, call *on_hotkey* on each press, then open Quick Entry.

    Returns 0 on a clean exit, 1 when the chord is taken, 2 on a bad chord or a
    non-Windows host. The loop outlives capture errors: the callback is expected
    to report its own failures, and anything that escapes it is shown here and
    ignored rather than ending the process.
    """
    if sys.platform != "win32":
        print("Global hotkey mode is Windows-only today.")
        return 2
    try:
        mods, vk = parse_chord(hotkey)
        if entry_chord:
            parse_chord(entry_chord)
    except ValueError as exc:
        print(f"Bad hotkey: {exc}")
        return 2

    user32 = ctypes.windll.user32
    hotkey_id = 1
    if not user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, vk):
        message_box(f"{hotkey} is already taken by another application.")
        return 1

    print(f"Look At This: press {hotkey} in any app to ask about that window.")
    if entry_chord:
        print(f"Quick Entry chord: {entry_chord} (must match Hermes' setting)")

    msg = wintypes.MSG()
    try:
        while True:
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got == 0 or got == -1:  # WM_QUIT or a queue error
                break
            if msg.message != WM_HOTKEY or msg.wParam != hotkey_id:
                continue
            try:
                on_hotkey()
            except Exception as exc:  # never kill the hotkey loop
                message_box(f"{type(exc).__name__}: {exc}")
                continue
            if entry_chord:
                send_chord(entry_chord)
    finally:
        user32.UnregisterHotKey(None, hotkey_id)
    return 0


def _startup_lnk() -> Path:
    return (Path(os.environ.get("APPDATA", str(Path.home())))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            / "Look At This Companion.lnk")


def _ps_quote(value: object) -> str:
    """A path safe inside a PowerShell single-quoted string."""
    return str(value).replace("'", "''")


def install_autostart(script: Path) -> int:
    """Create a Startup shortcut that runs ``<script> --hotkey`` at login."""
    if sys.platform != "win32":
        print("Autostart is Windows-only today.")
        return 2
    script = Path(script).resolve()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    lnk = _startup_lnk()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    ps1 = Path(tempfile.gettempdir()) / "lat_autostart.ps1"
    ps1.write_text(
        "$ws = New-Object -ComObject WScript.Shell\n"
        f"$s = $ws.CreateShortcut('{_ps_quote(lnk)}')\n"
        f"$s.TargetPath = '{_ps_quote(exe)}'\n"
        f"$s.Arguments = '{_ps_quote(f'\"{script}\" --hotkey')}'\n"
        f"$s.WorkingDirectory = '{_ps_quote(script.parent)}'\n"
        "$s.Save()\n",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
            capture_output=True, text=True,
        )
    except OSError as exc:
        print(f"Could not run PowerShell: {exc}")
        return 1
    finally:
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
