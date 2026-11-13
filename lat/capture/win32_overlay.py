"""Interactive hardware-GDI screen snipping & window-click selector for Windows.

Invoked in an isolated child process (``python -m lat.capture.win32_overlay``) so
Per-Monitor-V2 DPI awareness and the modal Win32 message loop never alter or
block the Hermes gateway process.

UX:
  - Hover any window -> highlights that window cleanly; click (<8px drag) to capture it!
  - Click and drag -> draws a live crosshair region selector with pixel dimensions;
    release to capture that exact screen rectangle!
  - Esc or Right-Click -> cancels cleanly.
"""

from __future__ import annotations

import ctypes
import json
from pathlib import Path
import sys
from typing import Any
import uuid

if sys.platform == "win32":
    import ctypes.wintypes as wt

from ..png_codec import bgra_to_thumbnail_data_url, crop_bgra, encode_bgra_png
from ..privacy import find_overlapping_excluded_window, unoccluded_excluded_rects


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


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


def _window_at(sx: int, sy: int, windows_meta: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the topmost visible window containing virtual-screen coordinates (sx, sy)."""
    for w in windows_meta:
        b = w.get("bounds") or {}
        bx, by, bw, bh = int(b.get("x", 0)), int(b.get("y", 0)), int(b.get("width", 0)), int(b.get("height", 0))
        if bx <= sx < bx + bw and by <= sy < by + bh:
            return w
    return None


def _resolve_snip_release(
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    vx: int,
    vy: int,
    vw: int,
    vh: int,
    windows_meta: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve click vs region-drag release in overlay coordinates, enforcing privacy exclusions."""
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    if dx < 8 and dy < 8:
        target = _window_at(x1 + vx, y1 + vy, windows_meta)
        if not target:
            return {"action": "cancel"}
        if target.get("is_excluded"):
            return {
                "action": "excluded",
                "app_display": target.get("app_display") or target.get("app") or "Protected window",
                "exclusion_reason": target.get("exclusion_reason") or "excluded window",
            }
        return {"action": "window", "window_id": target["window_id"]}

    rx0 = max(0, min(x0, x1))
    ry0 = max(0, min(y0, y1))
    rw = max(12, min(vw - rx0, dx))
    rh = max(12, min(vh - ry0, dy))
    region_screen = {"x": rx0 + vx, "y": ry0 + vy, "width": rw, "height": rh}
    bad = find_overlapping_excluded_window(region_screen, windows_meta)
    if bad is not None:
        return {
            "action": "excluded",
            "app_display": bad.get("app_display") or bad.get("app") or "Protected window",
            "exclusion_reason": bad.get("exclusion_reason") or "excluded window",
        }
    cx = rx0 + rw // 2 + vx
    cy = ry0 + rh // 2 + vy
    under = _window_at(cx, cy, windows_meta)
    if under and under.get("is_excluded"):
        under = None
    return {
        "action": "region",
        "rx0": rx0,
        "ry0": ry0,
        "rw": rw,
        "rh": rh,
        "under": under,
    }


def _run_win32_snip(shots_dir: Path, max_dim: int, windows_meta: list[dict[str, Any]]) -> dict[str, Any]:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32
    msimg32 = ctypes.windll.msimg32

    # Enable Per-Monitor V2 DPI awareness for 1:1 physical pixels across all monitors
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    vx = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
    vy = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
    vw = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
    vh = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
    if vw <= 0 or vh <= 0:
        vw = int(user32.GetSystemMetrics(0))
        vh = int(user32.GetSystemMetrics(1))
        vx, vy = 0, 0

    # 1. Snapshot clean virtual desktop BEFORE opening overlay
    hdc_screen = user32.GetDC(0)
    hdc_clean = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp_clean = gdi32.CreateCompatibleBitmap(hdc_screen, vw, vh)
    old_clean = gdi32.SelectObject(hdc_clean, hbmp_clean)
    SRCCOPY = 0x00CC0020
    CAPTUREBLT = 0x40000000
    gdi32.BitBlt(hdc_clean, 0, 0, vw, vh, hdc_screen, vx, vy, SRCCOPY | CAPTUREBLT)

    # Mask any visible unoccluded excluded window regions before dimming or reading pixels
    black_brush = gdi32.GetStockObject(4)  # BLACK_BRUSH
    mask_brush = gdi32.CreateSolidBrush(0x00181512)
    for ex_rect in unoccluded_excluded_rects({"x": vx, "y": vy, "width": vw, "height": vh}, windows_meta):
        mx0 = max(0, ex_rect.x - vx)
        my0 = max(0, ex_rect.y - vy)
        mx1 = min(vw, ex_rect.x + ex_rect.width - vx)
        my1 = min(vh, ex_rect.y + ex_rect.height - vy)
        if mx1 > mx0 and my1 > my0:
            mrc = wt.RECT(mx0, my0, mx1, my1)
            user32.FillRect(hdc_clean, ctypes.byref(mrc), mask_brush)
    gdi32.DeleteObject(mask_brush)

    # Also pre-build dimmed desktop DC once using hardware AlphaBlend (<1ms!)
    hdc_dim = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp_dim = gdi32.CreateCompatibleBitmap(hdc_screen, vw, vh)
    old_dim = gdi32.SelectObject(hdc_dim, hbmp_dim)
    gdi32.BitBlt(hdc_dim, 0, 0, vw, vh, hdc_clean, 0, 0, SRCCOPY)

    hdc_black = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp_black = gdi32.CreateCompatibleBitmap(hdc_screen, 1, 1)
    old_black = gdi32.SelectObject(hdc_black, hbmp_black)
    rc1 = wt.RECT(0, 0, 1, 1)
    user32.FillRect(hdc_black, ctypes.byref(rc1), black_brush)
    blend = _BLENDFUNCTION(0, 0, 120, 0)  # ~47% dim veil
    msimg32.AlphaBlend(hdc_dim, 0, 0, vw, vh, hdc_black, 0, 0, 1, 1, blend)

    # Backbuffer DC for tear-free 120fps painting
    hdc_back = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp_back = gdi32.CreateCompatibleBitmap(hdc_screen, vw, vh)
    old_back = gdi32.SelectObject(hdc_back, hbmp_back)

    # Extract raw BGRA bytes of clean desktop so region crops are pristine
    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = vw
    bmi.bmiHeader.biHeight = -vh  # top-down!
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    clean_bgra = ctypes.create_string_buffer(vw * vh * 4)
    gdi32.GetDIBits(hdc_clean, hbmp_clean, 0, vh, clean_bgra, ctypes.byref(bmi), 0)

    user32.ReleaseDC(0, hdc_screen)

    # Styling brushes & font
    accent_brush = gdi32.CreateSolidBrush(0x00F68B3B)  # BGR warm electric blue (#3B8BF6)
    blocked_brush = gdi32.CreateSolidBrush(0x004444EF)  # BGR warning red
    white_brush = gdi32.CreateSolidBrush(0x00FFFFFF)
    pill_bg_brush = gdi32.CreateSolidBrush(0x001E1A17)
    hfont = gdi32.CreateFontW(
        -16, 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI"
    )

    state: dict[str, Any] = {
        "dragging": False,
        "down": False,
        "x0": 0,
        "y0": 0,
        "x1": 0,
        "y1": 0,
        "mx": vw // 2,
        "my": vh // 2,
        "hovered": None,
        "result": {"cancelled": True},
    }

    pt_init = wt.POINT()
    if user32.GetCursorPos(ctypes.byref(pt_init)):
        state["mx"] = pt_init.x - vx
        state["my"] = pt_init.y - vy
        state["hovered"] = _window_at(pt_init.x, pt_init.y, windows_meta)

    def _draw_frame(hwnd_overlay: int) -> None:
        gdi32.BitBlt(hdc_back, 0, 0, vw, vh, hdc_dim, 0, 0, SRCCOPY)

        label_text = "Click any window to capture  •  Drag to snip region  •  Esc to cancel"
        rx0 = ry0 = rx1 = ry1 = 0
        has_highlight = False
        is_blocked = False

        if state["down"] and (abs(state["x1"] - state["x0"]) >= 8 or abs(state["y1"] - state["y0"]) >= 8):
            rx0 = max(0, min(state["x0"], state["x1"]))
            ry0 = max(0, min(state["y0"], state["y1"]))
            rx1 = min(vw, max(state["x0"], state["x1"]))
            ry1 = min(vh, max(state["y0"], state["y1"]))
            has_highlight = (rx1 > rx0) and (ry1 > ry0)
            bad = (
                find_overlapping_excluded_window(
                    {"x": rx0 + vx, "y": ry0 + vy, "width": rx1 - rx0, "height": ry1 - ry0},
                    windows_meta,
                )
                if has_highlight
                else None
            )
            if bad is not None:
                is_blocked = True
                label_text = "Protected window — capture blocked by privacy rules  •  Esc to cancel"
            else:
                label_text = f"Region {rx1 - rx0} × {ry1 - ry0} px  •  Release to capture  •  Esc to cancel"
        elif state["hovered"]:
            b = state["hovered"]["bounds"]
            rx0 = max(0, int(b["x"]) - vx)
            ry0 = max(0, int(b["y"]) - vy)
            rx1 = min(vw, int(b["x"]) + int(b["width"]) - vx)
            ry1 = min(vh, int(b["y"]) + int(b["height"]) - vy)
            has_highlight = (rx1 > rx0) and (ry1 > ry0)
            if state["hovered"].get("is_excluded"):
                is_blocked = True
                label_text = "Protected window — capture blocked by privacy rules  •  Esc to cancel"
            else:
                app_lbl = state["hovered"].get("app_display") or state["hovered"].get("app") or "Window"
                ttl = (state["hovered"].get("title") or "").strip()
                if len(ttl) > 48:
                    ttl = ttl[:47] + "…"
                label_text = f"{app_lbl}" + (f" — {ttl}" if ttl else "") + f" ({rx1 - rx0}×{ry1 - ry0})  •  Click to capture, or drag region"

        if has_highlight:
            rw = rx1 - rx0
            rh = ry1 - ry0
            if not is_blocked:
                gdi32.BitBlt(hdc_back, rx0, ry0, rw, rh, hdc_clean, rx0, ry0, SRCCOPY)
            frame_brush = blocked_brush if is_blocked else accent_brush
            for inset in (0, 1):
                rc = wt.RECT(rx0 - inset, ry0 - inset, rx1 + inset, ry1 + inset)
                user32.FrameRect(hdc_back, ctypes.byref(rc), frame_brush)

        # Draw top-center floating HUD banner pill on primary monitor area
        pill_w = min(vw - 40, max(440, len(label_text) * 8 + 44))
        pill_h = 36
        # Position near top of whichever monitor cursor is on (approx around mx)
        px = max(20, min(vw - pill_w - 20, state["mx"] - pill_w // 2))
        py = 24 if (not has_highlight or ry0 > 72) else min(vh - 56, ry1 + 14)
        pill_rc = wt.RECT(px, py, px + pill_w, py + pill_h)
        user32.FillRect(hdc_back, ctypes.byref(pill_rc), pill_bg_brush)
        user32.FrameRect(hdc_back, ctypes.byref(pill_rc), white_brush)

        old_f = gdi32.SelectObject(hdc_back, hfont)
        gdi32.SetBkMode(hdc_back, 1)  # TRANSPARENT
        gdi32.SetTextColor(hdc_back, 0x00F8F8F8)
        user32.DrawTextW(
            hdc_back,
            label_text,
            -1,
            ctypes.byref(pill_rc),
            0x00000001 | 0x00000004 | 0x00000020 | 0x00008000,  # DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS
        )
        gdi32.SelectObject(hdc_back, old_f)

        hdc_wnd = user32.GetDC(hwnd_overlay)
        gdi32.BitBlt(hdc_wnd, 0, 0, vw, vh, hdc_back, 0, 0, SRCCOPY)
        user32.ReleaseDC(hwnd_overlay, hdc_wnd)

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t

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

    class _PAINTSTRUCT(ctypes.Structure):
        _fields_ = [
            ("hdc", wt.HDC),
            ("fErase", wt.BOOL),
            ("rcPaint", wt.RECT),
            ("fRestore", wt.BOOL),
            ("fIncUpdate", wt.BOOL),
            ("rgbReserved", ctypes.c_byte * 32),
        ]

    cross_cursor = user32.LoadCursorW(None, ctypes.c_void_p(32515))  # IDC_CROSS

    def _wnd_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == 0x0020:  # WM_SETCURSOR
            user32.SetCursor(cross_cursor)
            return 1
        if msg == 0x0014:  # WM_ERASEBKGND
            return 1
        if msg == 0x000F:  # WM_PAINT
            ps = _PAINTSTRUCT()
            user32.BeginPaint(hwnd, ctypes.byref(ps))
            _draw_frame(hwnd)
            user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if msg == 0x0100:  # WM_KEYDOWN
            if wparam == 0x1B:  # VK_ESCAPE
                state["result"] = {"cancelled": True}
                user32.DestroyWindow(hwnd)
                return 0
        if msg == 0x0204:  # WM_RBUTTONDOWN
            state["result"] = {"cancelled": True}
            user32.DestroyWindow(hwnd)
            return 0
        if msg == 0x0201:  # WM_LBUTTONDOWN
            x = ctypes.c_short(lparam & 0xFFFF).value
            y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            state["down"] = True
            state["x0"] = state["x1"] = x
            state["y0"] = state["y1"] = y
            user32.SetCapture(hwnd)
            _draw_frame(hwnd)
            return 0
        if msg == 0x0200:  # WM_MOUSEMOVE
            x = ctypes.c_short(lparam & 0xFFFF).value
            y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            state["mx"] = x
            state["my"] = y
            if state["down"]:
                state["x1"] = x
                state["y1"] = y
            else:
                state["hovered"] = _window_at(x + vx, y + vy, windows_meta)
            _draw_frame(hwnd)
            return 0
        if msg == 0x0202:  # WM_LBUTTONUP
            if not state["down"]:
                return 0
            state["down"] = False
            user32.ReleaseCapture()
            x = ctypes.c_short(lparam & 0xFFFF).value
            y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            resolved = _resolve_snip_release(
                state["x0"], state["y0"], x, y, vx, vy, vw, vh, windows_meta
            )
            action = resolved.get("action")
            if action == "excluded":
                state["result"] = {
                    "cancelled": False,
                    "error": "excluded_window",
                    "app_display": resolved.get("app_display", "Protected window"),
                    "exclusion_reason": resolved.get("exclusion_reason", "excluded window"),
                }
            elif action == "window":
                state["result"] = {
                    "cancelled": False,
                    "mode": "window",
                    "window_id": resolved["window_id"],
                }
            elif action == "region":
                rx0 = int(resolved["rx0"])
                ry0 = int(resolved["ry0"])
                rw = int(resolved["rw"])
                rh = int(resolved["rh"])
                cropped, cw, ch = crop_bgra(clean_bgra.raw, vw, vh, rx0, ry0, rw, rh)
                cid = uuid.uuid4().hex[:12]
                out_path = shots_dir / f"lat_{cid}.png"
                png_bytes, out_w, out_h = encode_bgra_png(cropped, cw, ch, max_dim=max_dim)
                out_path.write_bytes(png_bytes)
                thumb = bgra_to_thumbnail_data_url(cropped, cw, ch, max_dim=360)
                state["result"] = {
                    "cancelled": False,
                    "mode": "region",
                    "capture_id": cid,
                    "bounds": {"x": rx0 + vx, "y": ry0 + vy, "width": cw, "height": ch},
                    "screenshot_path": str(out_path),
                    "screenshot_width": out_w,
                    "screenshot_height": out_h,
                    "thumbnail_data_url": thumb,
                    "under_window": resolved.get("under"),
                }
            else:
                state["result"] = {"cancelled": True}
            user32.DestroyWindow(hwnd)
            return 0
        if msg == 0x0002:  # WM_DESTROY
            user32.PostQuitMessage(0)
            return 0
        return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))

    proc_cb = WNDPROC(_wnd_proc)
    cls_name = f"LookAtThisSnip_{uuid.uuid4().hex[:8]}"
    hinst = kernel32.GetModuleHandleW(None)
    wc = _WNDCLASSEXW(
        ctypes.sizeof(_WNDCLASSEXW),
        0,
        proc_cb,
        0,
        0,
        hinst,
        None,
        cross_cursor,
        None,
        None,
        cls_name,
        None,
    )
    user32.RegisterClassExW(ctypes.byref(wc))

    WS_POPUP = 0x80000000
    WS_VISIBLE = 0x10000000
    WS_EX_TOPMOST = 0x00000008
    WS_EX_TOOLWINDOW = 0x00000080

    hwnd = user32.CreateWindowExW(
        WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
        cls_name,
        "Look At This Snip",
        WS_POPUP | WS_VISIBLE,
        vx,
        vy,
        vw,
        vh,
        None,
        None,
        hinst,
        None,
    )
    user32.ShowWindow(hwnd, 5)
    user32.SetForegroundWindow(hwnd)
    user32.SetFocus(hwnd)
    _draw_frame(hwnd)

    msg_obj = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg_obj), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg_obj))
        user32.DispatchMessageW(ctypes.byref(msg_obj))

    # Clean up GDI handles
    gdi32.SelectObject(hdc_clean, old_clean)
    gdi32.DeleteObject(hbmp_clean)
    gdi32.DeleteDC(hdc_clean)
    gdi32.SelectObject(hdc_dim, old_dim)
    gdi32.DeleteObject(hbmp_dim)
    gdi32.DeleteDC(hdc_dim)
    gdi32.SelectObject(hdc_black, old_black)
    gdi32.DeleteObject(hbmp_black)
    gdi32.DeleteDC(hdc_black)
    gdi32.SelectObject(hdc_back, old_back)
    gdi32.DeleteObject(hbmp_back)
    gdi32.DeleteDC(hdc_back)
    gdi32.DeleteObject(accent_brush)
    gdi32.DeleteObject(blocked_brush)
    gdi32.DeleteObject(white_brush)
    gdi32.DeleteObject(pill_bg_brush)
    gdi32.DeleteObject(hfont)
    user32.UnregisterClassW(cls_name, hinst)

    return state["result"]


def main() -> int:
    if sys.platform != "win32":
        print(json.dumps({"cancelled": True, "error": "win32 only"}))
        return 1
    payload = json.loads(sys.stdin.read() or "{}")
    shots_dir = Path(payload["shots_dir"])
    shots_dir.mkdir(parents=True, exist_ok=True)
    max_dim = int(payload.get("max_dim") or 1920)
    windows_meta = payload.get("windows") or []
    res = _run_win32_snip(shots_dir, max_dim, windows_meta)
    sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
