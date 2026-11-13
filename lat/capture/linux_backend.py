"""Linux (X11 / Wayland) capture backend using standard desktop utilities."""

from __future__ import annotations

import base64
from pathlib import Path
import shutil
import subprocess
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
from ..png_codec import read_png_dimensions
from ..privacy import check_window_exclusion
from ..store import get_shots_dir


def _png_meta(path: Path) -> tuple[int | None, int | None, str | None]:
    try:
        raw = path.read_bytes()
    except OSError:
        return None, None, None
    dims = read_png_dimensions(raw)
    w, h = dims if dims else (None, None)
    thumb = ("data:image/png;base64," + base64.b64encode(raw).decode("ascii")) if len(raw) <= 900_000 else None
    return w, h, thumb


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
                app = "linux-app"
                if pid:
                    comm = Path(f"/proc/{pid}/comm")
                    if comm.is_file():
                        app = comm.read_text(encoding="utf-8", errors="replace").strip() or app
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

    for w in out:
        if not w.is_excluded:
            w.is_active = True
            break
    return out


def _shoot_rect(rect: RegionRect, out_file: Path, wid: str | None = None) -> None:
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
    elif shutil.which("gnome-screenshot"):
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
        _shoot_rect(target.bounds, out_file, wid=str(target.window_id))
        if out_file.is_file():
            shot_path = str(out_file)
            w_px, h_px, thumb = _png_meta(out_file)

    return RawCapture(
        capture_id=cid,
        mode="window",
        app=target.app,
        pid=target.pid,
        window_id=target.window_id,
        title=target.title,
        bounds=target.bounds,
        selection=_read_selection_x11_wayland(),
        screenshot_path=shot_path,
        screenshot_width=w_px,
        screenshot_height=h_px,
        thumbnail_data_url=thumb,
    )


def capture_region_rect(rect: RegionRect, settings: Settings, *, include_tree: bool = False) -> RawCapture:
    cid = uuid.uuid4().hex[:12]
    out_file = get_shots_dir() / f"lat_{cid}.png"
    _shoot_rect(rect, out_file)
    if not out_file.is_file():
        raise CaptureBackendError("Linux screenshot capture failed.")
    w_px, h_px, thumb = _png_meta(out_file)
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
    cid = uuid.uuid4().hex[:12]
    out_file = get_shots_dir() / f"lat_{cid}.png"
    if shutil.which("grim") and shutil.which("slurp"):
        slurp = subprocess.run(["slurp"], capture_output=True, text=True, timeout=120)
        geom = (slurp.stdout or "").strip()
        if slurp.returncode != 0 or not geom:
            raise CaptureCancelledError("Capture cancelled.")
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
    w_px, h_px, thumb = _png_meta(out_file)
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
