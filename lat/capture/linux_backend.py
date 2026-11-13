"""Linux (X11 / Wayland) capture backend using standard desktop utilities."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any
import uuid

from ..models import (
    CaptureBackendError,
    CaptureCancelledError,
    ExcludedWindowError,
    NoWindowFoundError,
    RawCapture,
    RegionRect,
    Settings,
    WindowInfo,
)
from ..png_codec import postprocess_png_file
from ..privacy import check_window_exclusion, find_overlapping_excluded_window
from ..store import get_shots_dir


def _png_meta(path: Path, *, max_dim: int = 0) -> tuple[int | None, int | None, str | None]:
    return postprocess_png_file(path, max_dim=max_dim)


def _linux_process_name(pid: int) -> str:
    """Resolve full Linux process executable name without 15-char /proc/comm truncation."""
    if pid <= 0:
        return "linux-app"
    exe_link = Path(f"/proc/{pid}/exe")
    try:
        target = os.readlink(exe_link)
        name = Path(target.removesuffix(" (deleted)")).name.strip()
        if name:
            return name
    except OSError:
        pass
    cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        if cmdline.is_file():
            raw = cmdline.read_bytes().split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()
            if raw:
                name = Path(raw).name.strip()
                if name:
                    return name
    except OSError:
        pass
    comm = Path(f"/proc/{pid}/comm")
    try:
        if comm.is_file():
            name = comm.read_text(encoding="utf-8", errors="replace").strip()
            if name:
                return name
    except OSError:
        pass
    return "linux-app"


def _parse_xid(raw: Any) -> int | None:
    try:
        s = str(raw or "").strip()
        if not s:
            return None
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except ValueError:
        return None


def _x11_active_window_id() -> int | None:
    if not shutil.which("xprop"):
        return None
    try:
        res = subprocess.run(
            ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        out = (res.stdout or "").strip()
        # Format: "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x4a00007"
        for tok in reversed(out.replace(",", " ").split()):
            if tok.lower().startswith("0x"):
                xid = _parse_xid(tok)
                if xid:
                    return xid
    except Exception:
        pass
    return None


def _read_selection_x11_wayland() -> str | None:
    for cmd in (["wl-paste", "--primary", "--no-newline"], ["xclip", "-o", "-selection", "primary"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=1.5)
            txt = (res.stdout or "").strip()
            if txt:
                return txt[:4000]
        except Exception:
            continue
    return None


def list_windows(settings: Settings, *, include_thumbnails: bool = False) -> list[WindowInfo]:
    out: list[WindowInfo] = []
    if shutil.which("wmctrl"):
        try:
            proc = subprocess.run(["wmctrl", "-lpG"], capture_output=True, text=True, timeout=3)
            lines = (proc.stdout or "").splitlines()
            for idx, line in enumerate(reversed(lines)):
                parts = line.split(None, 8)
                if len(parts) < 9:
                    continue
                wid, _desk, pid_s, x_s, y_s, w_s, h_s, _host, title = parts
                pid = int(pid_s) if pid_s.isdigit() else 0
                w, h = int(w_s), int(h_s)
                if w < 80 or h < 60 or not title.strip():
                    continue
                app = _linux_process_name(pid)
                if app.lower() == "hermes":
                    continue
                rule = check_window_exclusion(app, title, settings)
                out.append(
                    WindowInfo(
                        window_id=wid,
                        pid=pid,
                        app=app,
                        title=title.strip(),
                        bounds=RegionRect(int(x_s), int(y_s), w, h),
                        z_order=idx,
                        is_excluded=bool(rule),
                        exclusion_reason=rule or "",
                    )
                )
        except Exception:
            pass

    active_xid = _x11_active_window_id()
    if active_xid is not None and out:
        for idx, w in enumerate(out):
            if _parse_xid(w.window_id) == active_xid:
                if idx > 0:
                    out.insert(0, out.pop(idx))
                break
    for idx, w in enumerate(out):
        w.z_order = idx

    for w in out:
        if not w.is_excluded:
            w.is_active = True
            break

    if include_thumbnails and (shutil.which("grim") or shutil.which("import")):
        with tempfile.TemporaryDirectory(prefix="lat-linthumb-") as tmpdir:
            for idx, w in enumerate(out[:12]):
                if w.is_excluded:
                    continue
                tmp_png = Path(tmpdir) / f"t_{idx}.png"
                try:
                    _shoot_rect(w.bounds, tmp_png, wid=str(w.window_id), allow_active_window_tool=False)
                    if tmp_png.is_file():
                        _, _, w.thumbnail_data_url = postprocess_png_file(tmp_png, max_dim=280, thumb_max_dim=280)
                except Exception:
                    w.thumbnail_data_url = None

    return out


def _shoot_rect(
    rect: RegionRect,
    out_file: Path,
    wid: str | None = None,
    *,
    allow_active_window_tool: bool = False,
) -> None:
    if shutil.which("grim"):
        subprocess.run(
            ["grim", "-g", f"{rect.x},{rect.y} {rect.width}x{rect.height}", str(out_file)],
            capture_output=True,
            timeout=8,
        )
    elif wid and shutil.which("import"):
        subprocess.run(["import", "-window", wid, str(out_file)], capture_output=True, timeout=8)
    elif shutil.which("import"):
        subprocess.run(
            ["import", "-window", "root", "-crop", f"{rect.width}x{rect.height}+{rect.x}+{rect.y}", str(out_file)],
            capture_output=True,
            timeout=8,
        )
    elif allow_active_window_tool and shutil.which("gnome-screenshot"):
        subprocess.run(["gnome-screenshot", "-w", "-f", str(out_file)], capture_output=True, timeout=8)
    else:
        raise CaptureBackendError(
            "No Linux screenshot utility found.",
            detail="Install `grim` + `slurp` (Wayland) or `imagemagick` + `wmctrl` (X11).",
        )


def capture_target_window(
    settings: Settings,
    *,
    window_id: int | str | None = None,
    hint: str = "",
    include_screenshot: bool = True,
    include_tree: bool = False,
) -> RawCapture:
    windows = list_windows(settings)
    if not windows:
        raise NoWindowFoundError(
            "No visible windows enumerated.",
            detail="Install `wmctrl` on X11 or use interactive region snip (`grim` + `slurp`) on Wayland.",
        )

    target = windows[0]
    if window_id is not None and str(window_id):
        found = [w for w in windows if str(w.window_id) == str(window_id)]
        if not found:
            raise NoWindowFoundError(f"Window {window_id!r} not found.")
        target = found[0]
    elif hint.strip():
        needle = hint.strip().lower()
        found = [w for w in windows if needle in w.app.lower() or needle in w.title.lower()]
        if not found:
            raise NoWindowFoundError(f"No open window matches {hint!r}.")
        target = found[0]

    if target.is_excluded:
        raise ExcludedWindowError(f"Refused to capture {target.app_display}: {target.exclusion_reason}.")

    cid = uuid.uuid4().hex[:12]
    shot_path = None
    w_px = h_px = None
    thumb = None
    if include_screenshot:
        out_file = get_shots_dir() / f"lat_{cid}.png"
        _shoot_rect(
            target.bounds,
            out_file,
            wid=str(target.window_id),
            allow_active_window_tool=bool(target.is_active),
        )
        if out_file.is_file():
            shot_path = str(out_file)
            w_px, h_px, thumb = _png_meta(out_file, max_dim=settings.max_image_dim)

    return RawCapture(
        capture_id=cid,
        mode="window",
        app=target.app,
        pid=target.pid,
        window_id=target.window_id,
        title=target.title,
        bounds=target.bounds,
        selection=_read_selection_x11_wayland() if target.is_active else None,
        screenshot_path=shot_path,
        screenshot_width=w_px,
        screenshot_height=h_px,
        thumbnail_data_url=thumb,
    )


def _parse_slurp_geom(geom: str) -> RegionRect | None:
    try:
        pos_s, size_s = geom.strip().split(None, 1)
        x_s, y_s = pos_s.split(",", 1)
        w_s, h_s = size_s.lower().split("x", 1)
        w, h = int(w_s), int(h_s)
        if w <= 0 or h <= 0:
            return None
        return RegionRect(x=int(x_s), y=int(y_s), width=w, height=h)
    except Exception:
        return None


def capture_region_rect(rect: RegionRect, settings: Settings, *, include_tree: bool = False) -> RawCapture:
    windows = list_windows(settings)
    bad = find_overlapping_excluded_window(rect, windows)
    if bad is not None:
        app_lbl = bad.app_display if isinstance(bad, WindowInfo) else (bad.get("app_display") or "Protected window")
        reason = bad.exclusion_reason if isinstance(bad, WindowInfo) else (bad.get("exclusion_reason") or "excluded window")
        raise ExcludedWindowError(f"Refused to capture region over {app_lbl}: {reason}.")

    cid = uuid.uuid4().hex[:12]
    out_file = get_shots_dir() / f"lat_{cid}.png"
    _shoot_rect(rect, out_file)
    if not out_file.is_file():
        raise CaptureBackendError("Linux screenshot capture failed.")
    w_px, h_px, thumb = _png_meta(out_file, max_dim=settings.max_image_dim)
    return RawCapture(
        capture_id=cid,
        mode="region",
        app="screen",
        pid=0,
        window_id=None,
        title=f"Screen region ({rect.width}×{rect.height})",
        bounds=rect,
        selection=_read_selection_x11_wayland(),
        screenshot_path=str(out_file),
        screenshot_width=w_px,
        screenshot_height=h_px,
        thumbnail_data_url=thumb,
    )


def interactive_snip(settings: Settings, *, include_tree: bool = False) -> RawCapture:
    windows = list_windows(settings)
    for w in windows:
        if w.is_excluded and find_overlapping_excluded_window(w.bounds, windows) is not None:
            raise ExcludedWindowError(
                f"Refused interactive snip while protected window {w.app_display} is visible: {w.exclusion_reason}."
            )

    cid = uuid.uuid4().hex[:12]
    out_file = get_shots_dir() / f"lat_{cid}.png"
    if shutil.which("grim") and shutil.which("slurp"):
        slurp = subprocess.run(["slurp"], capture_output=True, text=True, timeout=120)
        geom = (slurp.stdout or "").strip()
        if slurp.returncode != 0 or not geom:
            raise CaptureCancelledError("Capture cancelled.")
        parsed = _parse_slurp_geom(geom)
        if parsed is not None:
            bad = find_overlapping_excluded_window(parsed, windows)
            if bad is not None:
                app_lbl = bad.app_display if isinstance(bad, WindowInfo) else (bad.get("app_display") or "Protected window")
                reason = bad.exclusion_reason if isinstance(bad, WindowInfo) else (bad.get("exclusion_reason") or "excluded window")
                raise ExcludedWindowError(f"Refused to capture region over {app_lbl}: {reason}.")
        subprocess.run(["grim", "-g", geom, str(out_file)], capture_output=True, timeout=10)
    elif shutil.which("import"):
        subprocess.run(["import", str(out_file)], capture_output=True, timeout=120)
    elif shutil.which("gnome-screenshot"):
        subprocess.run(["gnome-screenshot", "-a", "-f", str(out_file)], capture_output=True, timeout=120)
    else:
        raise CaptureBackendError(
            "No interactive snipping tool found.",
            detail="Install `grim` + `slurp` (Wayland) or `imagemagick` (X11).",
        )

    if not out_file.is_file():
        raise CaptureCancelledError("Capture cancelled.")
    w_px, h_px, thumb = _png_meta(out_file, max_dim=settings.max_image_dim)
    return RawCapture(
        capture_id=cid,
        mode="region",
        app="screen",
        pid=0,
        window_id=None,
        title=f"Interactive snip ({w_px or '?'}×{h_px or '?'})",
        bounds=RegionRect(0, 0, w_px or 1, h_px or 1),
        selection=_read_selection_x11_wayland(),
        screenshot_path=str(out_file),
        screenshot_width=w_px,
        screenshot_height=h_px,
        thumbnail_data_url=thumb,
    )


def focus_hermes_window() -> bool:
    if shutil.which("wmctrl"):
        try:
            subprocess.run(["wmctrl", "-a", "Hermes"], capture_output=True, timeout=2)
            return True
        except Exception:
            return False
    return False
