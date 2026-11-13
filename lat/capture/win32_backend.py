"""Native Windows 10/11 capture backend (Win32 + DWM + PrintWindow + UIA).

Features:
  - Per-thread DPI awareness context so coordinates and screenshots are always
    crisp physical pixels without mutating global host-process DPI settings.
  - True Z-order window enumeration skipping DWM-cloaked UWP phantoms, shell
    overlays, and Hermes itself when grabbing the window underneath.
  - Sub-50ms ``PrintWindow(PW_RENDERFULLCONTENT)`` window capture with automatic
    virtual-desktop ``BitBlt`` fallback for GPU-exclusive surfaces.
  - Region cropping and interactive fullscreen hover/drag snipping via
    ``win32_overlay``.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Iterator
import uuid

if sys.platform == "win32":
    import ctypes.wintypes as wt

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
from ..png_codec import (
    bgra_to_thumbnail_data_url,
    buffer_is_blank,
    crop_bgra,
    encode_bgra_png,
)
from ..privacy import check_window_exclusion
from ..store import get_shots_dir
from .uia_worker import UIA

# System / shell surfaces that are never a user application window
_SHELL_IGNORED_EXES = frozenset({
    "dwm.exe",
    "shellexperiencehost.exe",
    "startmenuexperiencehost.exe",
    "searchhost.exe",
    "searchapp.exe",
    "textinputhost.exe",
    "lockapp.exe",
    "applicationframehost.exe",
    "systemsettingsbroker.exe",
    "widgets.exe",
    "cua-driver.exe",
    "cua.exe",
})

_HERMES_EXES = frozenset({"hermes.exe"})


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


@contextmanager
def _per_monitor_dpi_thread() -> Iterator[None]:
    """Temporarily switch ONLY the calling thread to Per-Monitor-V2 DPI awareness."""
    if sys.platform != "win32":
        yield
        return
    user32 = ctypes.windll.user32
    prev = None
    try:
        fn = getattr(user32, "SetThreadDpiAwarenessContext", None)
        if fn is not None:
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = ctypes.c_void_p
            prev = fn(ctypes.c_void_p(-4))  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    except Exception:
        prev = None
    try:
        yield
    finally:
        if prev:
            try:
                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(prev))
            except Exception:
                pass


def _process_exe_name(pid: int) -> str:
    if not pid:
        return ""
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not hproc:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(hproc)
    return ""


def _window_title(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = int(user32.GetWindowTextLengthW(hwnd))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 2)
    user32.GetWindowTextW(hwnd, buf, length + 2)
    return buf.value.strip()


def _is_dwm_cloaked(hwnd: int) -> bool:
    try:
        dwmapi = ctypes.windll.dwmapi
        cloaked = ctypes.c_int(0)
        DWMWA_CLOAKED = 14
        hr = dwmapi.DwmGetWindowAttribute(
            wt.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        return hr == 0 and cloaked.value != 0
    except Exception:
        return False


def _window_bounds(hwnd: int) -> RegionRect | None:
    rect = wt.RECT()
    try:
        dwmapi = ctypes.windll.dwmapi
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        hr = dwmapi.DwmGetWindowAttribute(
            wt.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
        )
        if hr != 0:
            ctypes.windll.user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect))
    except Exception:
        if not ctypes.windll.user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect)):
            return None

    w = int(rect.right - rect.left)
    h = int(rect.bottom - rect.top)
    if w < 80 or h < 60:
        return None
    return RegionRect(x=int(rect.left), y=int(rect.top), width=w, height=h)


def _is_hermes_window(app_exe: str, title: str) -> bool:
    low = app_exe.lower()
    if low in _HERMES_EXES:
        return True
    if low == "electron.exe" and "hermes" in title.lower():
        return True
    return False


def _ensure_unlocked_desktop() -> None:
    user32 = ctypes.windll.user32
    fg = int(user32.GetForegroundWindow() or 0)
    if fg:
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        exe = _process_exe_name(int(pid.value)).lower()
        title = _window_title(fg).lower()
        if exe == "lockapp.exe" or "windows default lock screen" in title:
            raise CaptureBackendError(
                "Desktop is currently locked — nothing was captured.",
                detail="Unlock your screen and try again.",
                code="screen_locked",
            )


def _capture_hwnd_bgra(hwnd: int, bounds: RegionRect) -> tuple[bytes, int, int]:
    """Capture a window's pixels via PrintWindow(PW_RENDERFULLCONTENT) with BitBlt fallback."""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rc = wt.RECT()
    user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rc))
    w = max(1, int(rc.right - rc.left))
    h = max(1, int(rc.bottom - rc.top))

    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old_obj = gdi32.SelectObject(hdc_mem, hbmp)

    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0

    try:
        ok = bool(user32.PrintWindow(wt.HWND(hwnd), hdc_mem, 2))  # PW_RENDERFULLCONTENT = 2
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
        raw = buf.raw

        if (not ok) or buffer_is_blank(raw):
            # Fallback to screen BitBlt over DWM extended bounds
            bw, bh = bounds.width, bounds.height
            if (bw, bh) != (w, h):
                gdi32.SelectObject(hdc_mem, old_obj)
                gdi32.DeleteObject(hbmp)
                w, h = bw, bh
                hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
                old_obj = gdi32.SelectObject(hdc_mem, hbmp)
                bmi.bmiHeader.biWidth = w
                bmi.bmiHeader.biHeight = -h
            SRCCOPY = 0x00CC0020
            CAPTUREBLT = 0x40000000
            gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, bounds.x, bounds.y, SRCCOPY | CAPTUREBLT)
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
            raw = buf.raw
        else:
            # Trim invisible DWM shadow margins when GetWindowRect > DwmExtendedFrameBounds
            dx = bounds.x - int(rc.left)
            dy = bounds.y - int(rc.top)
            if (dx > 0 or dy > 0) and bounds.width <= w and bounds.height <= h:
                cropped, cw, ch = crop_bgra(raw, w, h, max(0, dx), max(0, dy), bounds.width, bounds.height)
                return bytes(cropped), cw, ch

        return raw, w, h
    finally:
        gdi32.SelectObject(hdc_mem, old_obj)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)


def _capture_screen_rect_bgra(rect: RegionRect) -> tuple[bytes, int, int]:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    w, h = max(1, rect.width), max(1, rect.height)
    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old_obj = gdi32.SelectObject(hdc_mem, hbmp)
    try:
        SRCCOPY = 0x00CC0020
        CAPTUREBLT = 0x40000000
        gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, rect.x, rect.y, SRCCOPY | CAPTUREBLT)
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
        return buf.raw, w, h
    finally:
        gdi32.SelectObject(hdc_mem, old_obj)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)


def _child_texts_fallback(hwnd: int, limit: int = 16) -> list[str]:
    """Ultra-fast (<2ms) classic Win32 child control text fallback."""
    user32 = ctypes.windll.user32
    items: list[str] = []
    seen: set[str] = set()
    ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _cb(child: int, _lp: int) -> bool:
        if len(items) >= limit:
            return False
        if user32.IsWindowVisible(child):
            txt = _window_title(child)
            if 2 <= len(txt) <= 220 and txt not in seen:
                seen.add(txt)
                items.append(txt)
        return True

    cb = ENUMPROC(_cb)
    user32.EnumChildWindows(wt.HWND(hwnd), cb, 0)
    return items


def list_windows(
    settings: Settings,
    *,
    include_thumbnails: bool = False,
    include_hermes: bool = False,
) -> list[WindowInfo]:
    """Enumerate visible top-level windows in Z-order (front to back)."""
    with _per_monitor_dpi_thread():
        _ensure_unlocked_desktop()
        user32 = ctypes.windll.user32
        results: list[WindowInfo] = []
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        WS_EX_NOACTIVATE = 0x08000000

        ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        z_counter = 0

        def _enum_cb(hwnd: int, _lp: int) -> bool:
            nonlocal z_counter
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            ex_style = int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE))
            if (ex_style & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)) and not (ex_style & WS_EX_APPWINDOW):
                return True
            if _is_dwm_cloaked(hwnd):
                return True
            title = _window_title(hwnd)
            if not title or title.lower().startswith("cua.agentcursoroverlay"):
                return True
            bounds = _window_bounds(hwnd)
            if bounds is None:
                return True

            pid = wt.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pid_int = int(pid.value)
            exe = _process_exe_name(pid_int)
            if not exe or exe.lower() in _SHELL_IGNORED_EXES:
                return True
            if not include_hermes and _is_hermes_window(exe, title):
                return True

            rule = check_window_exclusion(exe, title, settings)
            info = WindowInfo(
                window_id=int(hwnd),
                pid=pid_int,
                app=exe,
                title=title,
                bounds=bounds,
                z_order=z_counter,
                is_excluded=bool(rule),
                exclusion_reason=rule or "",
            )
            z_counter += 1
            results.append(info)
            return True

        cb = ENUMPROC(_enum_cb)
        user32.EnumWindows(cb, 0)

        # Mark topmost non-excluded window as the default active target
        for w in results:
            if not w.is_excluded:
                w.is_active = True
                break

        if include_thumbnails:
            for w in results[:16]:
                if w.is_excluded:
                    continue
                try:
                    raw, cw, ch = _capture_hwnd_bgra(int(w.window_id), w.bounds)
                    w.thumbnail_data_url = bgra_to_thumbnail_data_url(raw, cw, ch, max_dim=280)
                except Exception:
                    w.thumbnail_data_url = None

        return results


def _build_window_capture(
    win: WindowInfo,
    settings: Settings,
    *,
    include_screenshot: bool = True,
    include_tree: bool = False,
) -> RawCapture:
    if win.is_excluded:
        raise ExcludedWindowError(
            f"Refused to capture {win.app_display} ({win.title!r}): matches {win.exclusion_reason}.",
            detail="Adjust excluded apps/patterns in Settings → Plugins → Look At This if you want to allow it.",
        )

    hwnd = int(win.window_id)
    cid = uuid.uuid4().hex[:12]
    shot_path: str | None = None
    shot_w: int | None = None
    shot_h: int | None = None
    thumb_url: str | None = None

    with _per_monitor_dpi_thread():
        if include_screenshot:
            bgra, cw, ch = _capture_hwnd_bgra(hwnd, win.bounds)
            png_bytes, out_w, out_h = encode_bgra_png(bgra, cw, ch, max_dim=settings.max_image_dim)
            out_file = get_shots_dir() / f"lat_{cid}.png"
            out_file.write_bytes(png_bytes)
            shot_path = str(out_file)
            shot_w, shot_h = out_w, out_h
            thumb_url = bgra_to_thumbnail_data_url(bgra, cw, ch, max_dim=360)

        url, selection, smart = UIA.inspect_window(
            hwnd,
            win.pid,
            win.app,
            include_tree=include_tree,
            max_elements=settings.max_tree_elements,
        )
        if not smart.highlights:
            smart.highlights = _child_texts_fallback(hwnd)

    return RawCapture(
        capture_id=cid,
        mode="window",
        app=win.app,
        pid=win.pid,
        window_id=hwnd,
        title=win.title,
        bounds=win.bounds,
        url=url,
        selection=selection,
        context_text=smart.render_compact(),
        tree_markdown=smart.tree_markdown,
        element_count=smart.element_count,
        tree_truncated=smart.truncated,
        screenshot_path=shot_path,
        screenshot_width=shot_w,
        screenshot_height=shot_h,
        thumbnail_data_url=thumb_url,
    )


def capture_target_window(
    settings: Settings,
    *,
    window_id: int | str | None = None,
    hint: str = "",
    include_screenshot: bool = True,
    include_tree: bool = False,
) -> RawCapture:
    windows = list_windows(settings, include_thumbnails=False)
    if not windows:
        raise NoWindowFoundError("No visible application window was found on screen.")

    if window_id is not None and str(window_id).strip() != "":
        try:
            target_hwnd = int(window_id)
        except (TypeError, ValueError) as exc:
            raise NoWindowFoundError(f"Invalid window id: {window_id!r}") from exc
        for w in windows:
            if int(w.window_id) == target_hwnd:
                return _build_window_capture(
                    w, settings, include_screenshot=include_screenshot, include_tree=include_tree
                )
        raise NoWindowFoundError(f"Window {target_hwnd} is no longer visible.")

    if hint.strip():
        needle = hint.strip().lower()
        matches = [
            w
            for w in windows
            if needle in w.app.lower() or needle in w.app_display.lower() or needle in w.title.lower()
        ]
        if not matches:
            available = ", ".join(f"{w.app_display} ({w.title[:40]})" for w in windows[:6])
            raise NoWindowFoundError(
                f"No open window matches {hint!r}.",
                detail=f"Visible windows: {available}" if available else "",
            )
        return _build_window_capture(
            matches[0], settings, include_screenshot=include_screenshot, include_tree=include_tree
        )

    # Default: topmost visible non-Hermes window! If the topmost window is an
    # excluded password manager / incognito browser, refuse closed rather than
    # silently capturing a window behind the password manager!
    top = windows[0]
    return _build_window_capture(
        top, settings, include_screenshot=include_screenshot, include_tree=include_tree
    )


def capture_region_rect(
    rect: RegionRect,
    settings: Settings,
    *,
    include_tree: bool = False,
) -> RawCapture:
    windows = list_windows(settings, include_thumbnails=False)
    cx = rect.x + rect.width // 2
    cy = rect.y + rect.height // 2
    under: WindowInfo | None = None
    for w in windows:
        b = w.bounds
        if b.x <= cx < b.x + b.width and b.y <= cy < b.y + b.height:
            under = w
            break

    if under and under.is_excluded:
        raise ExcludedWindowError(
            f"Refused to capture region over {under.app_display}: {under.exclusion_reason}."
        )

    cid = uuid.uuid4().hex[:12]
    with _per_monitor_dpi_thread():
        bgra, cw, ch = _capture_screen_rect_bgra(rect)
        png_bytes, out_w, out_h = encode_bgra_png(bgra, cw, ch, max_dim=settings.max_image_dim)
        out_path = get_shots_dir() / f"lat_{cid}.png"
        out_path.write_bytes(png_bytes)
        thumb = bgra_to_thumbnail_data_url(bgra, cw, ch, max_dim=360)

        url = None
        selection = None
        ctx_text = ""
        tree_md = ""
        el_count = 0
        trunc = False
        if under:
            url, selection, smart = UIA.inspect_window(
                int(under.window_id),
                under.pid,
                under.app,
                include_tree=include_tree,
                max_elements=settings.max_tree_elements,
            )
            ctx_text = smart.render_compact()
            tree_md = smart.tree_markdown
            el_count = smart.element_count
            trunc = smart.truncated

    return RawCapture(
        capture_id=cid,
        mode="region",
        app=under.app if under else "screen",
        pid=under.pid if under else 0,
        window_id=under.window_id if under else None,
        title=under.title if under else f"Screen region ({rect.width}×{rect.height})",
        bounds=rect,
        url=url,
        selection=selection,
        context_text=ctx_text,
        tree_markdown=tree_md,
        element_count=el_count,
        tree_truncated=trunc,
        screenshot_path=str(out_path),
        screenshot_width=out_w,
        screenshot_height=out_h,
        thumbnail_data_url=thumb,
    )


def interactive_snip(settings: Settings, *, include_tree: bool = False) -> RawCapture:
    """Launch the fullscreen interactive window-hover / region-drag overlay."""
    windows = list_windows(settings, include_thumbnails=False)
    plugin_root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (plugin_root, env.get("PYTHONPATH", "")) if p)

    payload = {
        "shots_dir": str(get_shots_dir()),
        "max_dim": settings.max_image_dim,
        "windows": [w.to_dict() for w in windows],
    }
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "lat.capture.win32_overlay"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env=env,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired as exc:
        raise CaptureCancelledError("Snip cancelled (timed out waiting for selection).") from exc
    except OSError as exc:
        raise CaptureBackendError(f"Could not launch snipping overlay: {exc}") from exc

    raw_out = (proc.stdout or "").strip().splitlines()
    if not raw_out:
        raise CaptureCancelledError("Snip cancelled.")
    try:
        res = json.loads(raw_out[-1])
    except ValueError as exc:
        raise CaptureBackendError("Snipping overlay returned unreadable output.") from exc

    if res.get("cancelled"):
        raise CaptureCancelledError("Capture cancelled.")

    if res.get("mode") == "window" and res.get("window_id") is not None:
        return capture_target_window(
            settings,
            window_id=res["window_id"],
            include_screenshot=True,
            include_tree=include_tree,
        )

    under_dict = res.get("under_window")
    url = None
    selection = None
    ctx_text = ""
    tree_md = ""
    el_count = 0
    trunc = False
    app_name = "screen"
    pid = 0
    win_id = None
    bounds = RegionRect.from_dict(res.get("bounds"))
    title = f"Screen region ({bounds.width}×{bounds.height})" if bounds else "Screen region"

    if isinstance(under_dict, dict) and under_dict.get("window_id"):
        app_name = str(under_dict.get("app") or "screen")
        pid = int(under_dict.get("pid") or 0)
        win_id = under_dict.get("window_id")
        title = str(under_dict.get("title") or title)
        url, selection, smart = UIA.inspect_window(
            int(win_id),
            pid,
            app_name,
            include_tree=include_tree,
            max_elements=settings.max_tree_elements,
        )
        ctx_text = smart.render_compact()
        tree_md = smart.tree_markdown
        el_count = smart.element_count
        trunc = smart.truncated

    return RawCapture(
        capture_id=str(res.get("capture_id") or uuid.uuid4().hex[:12]),
        mode="region",
        app=app_name,
        pid=pid,
        window_id=win_id,
        title=title,
        bounds=bounds,
        url=url,
        selection=selection,
        context_text=ctx_text,
        tree_markdown=tree_md,
        element_count=el_count,
        tree_truncated=trunc,
        screenshot_path=res.get("screenshot_path"),
        screenshot_width=res.get("screenshot_width"),
        screenshot_height=res.get("screenshot_height"),
        thumbnail_data_url=res.get("thumbnail_data_url"),
    )


def focus_hermes_window() -> bool:
    """Bring the Hermes Desktop window cleanly to the foreground."""
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    target_hwnd = 0

    ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _find_cb(hwnd: int, _lp: int) -> bool:
        nonlocal target_hwnd
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not title:
            return True
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _process_exe_name(int(pid.value))
        if _is_hermes_window(exe, title):
            target_hwnd = int(hwnd)
            return False
        return True

    cb = ENUMPROC(_find_cb)
    user32.EnumWindows(cb, 0)
    if not target_hwnd:
        return False

    if user32.IsIconic(target_hwnd):
        user32.ShowWindow(target_hwnd, 9)  # SW_RESTORE

    if user32.SetForegroundWindow(target_hwnd):
        return True

    # Fallback thread-input handoff when called outside a WM_HOTKEY message
    fg = int(user32.GetForegroundWindow() or 0)
    cur_tid = int(kernel32.GetCurrentThreadId())
    fg_tid = int(user32.GetWindowThreadProcessId(fg, None)) if fg else 0
    attached = False
    try:
        if fg_tid and fg_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        user32.BringWindowToTop(target_hwnd)
        return bool(user32.SetForegroundWindow(target_hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)


def show_capture_feedback(label: str) -> None:
    """Display a brief, click-through, non-activating OS HUD pill confirming hotkey capture."""
    if sys.platform != "win32" or "unittest" in sys.modules:
        return
    import threading
    import time

    text = f"Look At This  •  {label}"

    def _toast_worker() -> None:
        with _per_monitor_dpi_thread():
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            kernel32 = ctypes.windll.kernel32
            try:
                sw = int(user32.GetSystemMetrics(0))
                if sw <= 0:
                    return
                pill_w = min(sw - 60, max(320, len(text) * 9 + 52))
                pill_h = 40
                px = (sw - pill_w) // 2
                py = 28

                WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
                user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
                user32.DefWindowProcW.restype = ctypes.c_ssize_t

                bg_brush = gdi32.CreateSolidBrush(0x00201B16)
                border_brush = gdi32.CreateSolidBrush(0x00F68B3B)
                hfont = gdi32.CreateFontW(-16, 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")

                class _PAINTSTRUCT(ctypes.Structure):
                    _fields_ = [
                        ("hdc", wt.HDC),
                        ("fErase", wt.BOOL),
                        ("rcPaint", wt.RECT),
                        ("fRestore", wt.BOOL),
                        ("fIncUpdate", wt.BOOL),
                        ("rgbReserved", ctypes.c_byte * 32),
                    ]

                def _wnd_proc(hwnd: int, msg: int, wp: int, lp: int) -> int:
                    if msg == 0x000F:  # WM_PAINT
                        ps = _PAINTSTRUCT()
                        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
                        rc = wt.RECT(0, 0, pill_w, pill_h)
                        user32.FillRect(hdc, ctypes.byref(rc), bg_brush)
                        for inset in (0, 1):
                            frc = wt.RECT(inset, inset, pill_w - inset, pill_h - inset)
                            user32.FrameRect(hdc, ctypes.byref(frc), border_brush)
                        old_f = gdi32.SelectObject(hdc, hfont)
                        gdi32.SetBkMode(hdc, 1)
                        gdi32.SetTextColor(hdc, 0x00FAF8F5)
                        user32.DrawTextW(
                            hdc,
                            text,
                            -1,
                            ctypes.byref(rc),
                            0x00000001 | 0x00000004 | 0x00000020 | 0x00008000,
                        )
                        gdi32.SelectObject(hdc, old_f)
                        user32.EndPaint(hwnd, ctypes.byref(ps))
                        return 0
                    return int(user32.DefWindowProcW(hwnd, msg, wp, lp))

                class _WNDCLASSEXW(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", wt.UINT),
                        ("style", wt.UINT),
                        ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wt.HINSTANCE),
                        ("hIcon", wt.HANDLE),
                        ("hCursor", wt.HANDLE),
                        ("hbrBackground", wt.HANDLE),
                        ("lpszMenuName", wt.LPCWSTR),
                        ("lpszClassName", wt.LPCWSTR),
                        ("hIconSm", wt.HANDLE),
                    ]

                proc_ref = WNDPROC(_wnd_proc)
                cls_name = f"LookAtThisHud_{uuid.uuid4().hex[:8]}"
                hinst = kernel32.GetModuleHandleW(None)
                wc = _WNDCLASSEXW()
                wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
                wc.lpfnWndProc = proc_ref
                wc.hInstance = hinst
                wc.lpszClassName = cls_name
                user32.RegisterClassExW(ctypes.byref(wc))

                # WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
                ex_style = 0x00080000 | 0x00000020 | 0x00000008 | 0x00000080 | 0x08000000
                ws_popup = 0x80000000
                hwnd = user32.CreateWindowExW(
                    ex_style, cls_name, "", ws_popup, px, py, pill_w, pill_h, None, None, hinst, None
                )
                if hwnd:
                    user32.SetLayeredWindowAttributes(hwnd, 0, 238, 0x02)  # LWA_ALPHA
                    user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
                    user32.UpdateWindow(hwnd)
                    msg = wt.MSG()
                    end_hold = time.monotonic() + 0.85
                    while time.monotonic() < end_hold:
                        while user32.PeekMessageW(ctypes.byref(msg), hwnd, 0, 0, 1):
                            user32.TranslateMessage(ctypes.byref(msg))
                            user32.DispatchMessageW(ctypes.byref(msg))
                        time.sleep(0.03)
                    for alpha in (190, 135, 80, 30):
                        user32.SetLayeredWindowAttributes(hwnd, 0, alpha, 0x02)
                        time.sleep(0.03)
                    user32.DestroyWindow(hwnd)
                user32.UnregisterClassW(cls_name, hinst)
                gdi32.DeleteObject(bg_brush)
                gdi32.DeleteObject(border_brush)
                gdi32.DeleteObject(hfont)
            except Exception:
                pass

    threading.Thread(target=_toast_worker, daemon=True, name="lat-hud-toast").start()
