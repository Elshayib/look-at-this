"""macOS capture backend using native Quartz / JXA / screencapture utilities."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
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
from ..png_codec import postprocess_png_file, read_png_dimensions
from ..privacy import check_window_exclusion, find_overlapping_excluded_window
from ..store import get_shots_dir

_JXA_WINDOWS = r"""
const se = Application('System Events');
const procs = se.applicationProcesses.whose({ visible: true })();
const procList = [];
for (const p of procs) {
  procList.push(p);
}
procList.sort(function(a, b) {
  let fa = 0;
  let fb = 0;
  try { fa = a.frontmost() ? 1 : 0; } catch (e) {}
  try { fb = b.frontmost() ? 1 : 0; } catch (e) {}
  return fb - fa;
});
const out = [];
let z = 0;
for (const p of procList) {
  let name = '';
  let pid = 0;
  let isFront = false;
  try { name = p.name(); pid = p.unixId(); } catch (e) { continue; }
  try { isFront = Boolean(p.frontmost()); } catch (e) {}
  if (!name || name.toLowerCase() === 'hermes') continue;
  let wins = [];
  try { wins = p.windows(); } catch (e) { continue; }
  for (const w of wins) {
    try {
      const title = String(w.name() || '').trim();
      const pos = w.position();
      const sz = w.size();
      if (!title || sz[0] < 80 || sz[1] < 60) continue;
      out.push({
        window_id: `${pid}:${z}`,
        pid: pid,
        app: name,
        title: title,
        frontmost: isFront,
        bounds: { x: pos[0], y: pos[1], width: sz[0], height: sz[1] },
        z_order: z++
      });
    } catch (e) {}
  }
}
JSON.stringify(out);
"""


def _png_thumb(path_str: str, *, max_dim: int = 0) -> tuple[int | None, int | None, str | None]:
    path = Path(path_str)
    if max_dim > 0 and shutil.which("sips") and path.is_file():
        try:
            dims = read_png_dimensions(path.read_bytes())
            if dims and max(dims) > max_dim:
                subprocess.run(
                    ["sips", "-Z", str(int(max_dim)), str(path)],
                    capture_output=True,
                    timeout=8,
                )
        except Exception:
            pass
    return postprocess_png_file(path, max_dim=max_dim)


def _macos_browser_url(app_name: str) -> str | None:
    low = app_name.lower()
    safe_app = str(app_name or "").replace("\\", "\\\\").replace('"', '\\"')
    script = ""
    if "chrome" in low or "brave" in low or "edge" in low or "arc" in low:
        script = f'tell application "{safe_app}" to get URL of active tab of front window'
    elif "safari" in low:
        script = 'tell application "Safari" to get URL of current tab of front window'
    if not script:
        return None
    try:
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=2)
        val = (res.stdout or "").strip()
        return val if val.startswith(("http://", "https://")) else None
    except Exception:
        return None


def list_windows(settings: Settings, *, include_thumbnails: bool = False) -> list[WindowInfo]:
    try:
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", _JXA_WINDOWS],
            capture_output=True,
            text=True,
            timeout=5,
        )
        rows = json.loads((proc.stdout or "[]").strip() or "[]")
    except Exception as exc:
        raise CaptureBackendError(
            f"macOS window enumeration failed: {exc}",
            detail="Grant Hermes Accessibility & Screen Recording permissions in System Settings → Privacy & Security.",
        ) from exc

    out: list[WindowInfo] = []
    for row in rows:
        bounds = RegionRect.from_dict(row.get("bounds"))
        if not bounds:
            continue
        app = str(row.get("app") or "unknown")
        title = str(row.get("title") or "")
        rule = check_window_exclusion(app, title, settings)
        out.append(
            WindowInfo(
                window_id=str(row.get("window_id")),
                pid=int(row.get("pid") or 0),
                app=app,
                title=title,
                bounds=bounds,
                z_order=int(row.get("z_order") or 0),
                is_excluded=bool(rule),
                exclusion_reason=rule or "",
            )
        )
    for w in out:
        if not w.is_excluded:
            w.is_active = True
            break
    if include_thumbnails and shutil.which("screencapture"):
        with tempfile.TemporaryDirectory(prefix="lat-macthumb-") as tmpdir:
            for idx, w in enumerate(out[:12]):
                if w.is_excluded:
                    continue
                tmp_png = Path(tmpdir) / f"t_{idx}.png"
                try:
                    b = w.bounds
                    subprocess.run(
                        ["screencapture", "-x", "-R", f"{b.x},{b.y},{b.width},{b.height}", str(tmp_png)],
                        capture_output=True,
                        timeout=4,
                    )
                    if tmp_png.is_file():
                        _, _, w.thumbnail_data_url = postprocess_png_file(tmp_png, max_dim=280, thumb_max_dim=280)
                except Exception:
                    w.thumbnail_data_url = None
    return out


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
        raise NoWindowFoundError("No visible application window was found.")

    target = windows[0]
    if window_id is not None and str(window_id):
        found = [w for w in windows if str(w.window_id) == str(window_id)]
        if not found:
            raise NoWindowFoundError(f"Window {window_id!r} is no longer visible.")
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
        b = target.bounds
        subprocess.run(
            ["screencapture", "-x", "-R", f"{b.x},{b.y},{b.width},{b.height}", str(out_file)],
            capture_output=True,
            timeout=10,
        )
        if out_file.is_file():
            shot_path = str(out_file)
            w_px, h_px, thumb = _png_thumb(shot_path, max_dim=settings.max_image_dim)

    return RawCapture(
        capture_id=cid,
        mode="window",
        app=target.app,
        pid=target.pid,
        window_id=target.window_id,
        title=target.title,
        bounds=target.bounds,
        url=_macos_browser_url(target.app),
        screenshot_path=shot_path,
        screenshot_width=w_px,
        screenshot_height=h_px,
        thumbnail_data_url=thumb,
    )


def capture_region_rect(rect: RegionRect, settings: Settings, *, include_tree: bool = False) -> RawCapture:
    windows = list_windows(settings)
    bad = find_overlapping_excluded_window(rect, windows)
    if bad is not None:
        app_lbl = bad.app_display if isinstance(bad, WindowInfo) else (bad.get("app_display") or "Protected window")
        reason = bad.exclusion_reason if isinstance(bad, WindowInfo) else (bad.get("exclusion_reason") or "excluded window")
        raise ExcludedWindowError(f"Refused to capture region over {app_lbl}: {reason}.")

    cid = uuid.uuid4().hex[:12]
    out_file = get_shots_dir() / f"lat_{cid}.png"
    subprocess.run(
        ["screencapture", "-x", "-R", f"{rect.x},{rect.y},{rect.width},{rect.height}", str(out_file)],
        capture_output=True,
        timeout=10,
    )
    if not out_file.is_file():
        raise CaptureBackendError("macOS screencapture produced no image (check Screen Recording permission).")
    w_px, h_px, thumb = _png_thumb(str(out_file), max_dim=settings.max_image_dim)
    return RawCapture(
        capture_id=cid,
        mode="region",
        app="screen",
        pid=0,
        window_id=None,
        title=f"Screen region ({rect.width}×{rect.height})",
        bounds=rect,
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
    subprocess.run(["screencapture", "-i", "-x", str(out_file)], capture_output=True, timeout=120)
    if not out_file.is_file():
        raise CaptureCancelledError("Capture cancelled.")
    w_px, h_px, thumb = _png_thumb(str(out_file), max_dim=settings.max_image_dim)
    return RawCapture(
        capture_id=cid,
        mode="region",
        app="screen",
        pid=0,
        window_id=None,
        title=f"Interactive snip ({w_px or '?'}×{h_px or '?'})",
        bounds=RegionRect(0, 0, w_px or 1, h_px or 1),
        screenshot_path=str(out_file),
        screenshot_width=w_px,
        screenshot_height=h_px,
        thumbnail_data_url=thumb,
    )


def focus_hermes_window() -> bool:
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Hermes" to activate'],
            capture_output=True,
            timeout=2,
        )
        return True
    except Exception:
        return False
