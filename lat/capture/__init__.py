"""Unified cross-platform capture facade."""

from __future__ import annotations

import sys

from ..models import CaptureBackendError, RawCapture, RegionRect, Settings, WindowInfo


def _backend():
    if sys.platform == "win32":
        from . import win32_backend as mod

        return mod
    if sys.platform == "darwin":
        from . import macos_backend as mod

        return mod
    if sys.platform.startswith("linux"):
        from . import linux_backend as mod

        return mod
    raise CaptureBackendError(f"Unsupported host platform: {sys.platform}")


def warmup() -> None:
    """Warm optional background helpers asynchronously (no-op off Windows or under unittest)."""
    if sys.platform == "win32" and "unittest" not in sys.modules:
        from .uia_worker import UIA

        UIA.warmup_async()


def list_windows(settings: Settings, *, include_thumbnails: bool = False) -> list[WindowInfo]:
    return _backend().list_windows(settings, include_thumbnails=include_thumbnails)


def capture_window(
    settings: Settings,
    *,
    window_id: int | str | None = None,
    hint: str = "",
    include_screenshot: bool = True,
    include_tree: bool = False,
) -> RawCapture:
    return _backend().capture_target_window(
        settings,
        window_id=window_id,
        hint=hint,
        include_screenshot=include_screenshot,
        include_tree=include_tree,
    )


def capture_region(rect: RegionRect, settings: Settings, *, include_tree: bool = False) -> RawCapture:
    return _backend().capture_region_rect(rect, settings, include_tree=include_tree)


def capture_interactive_snip(settings: Settings, *, include_tree: bool = False) -> RawCapture:
    return _backend().interactive_snip(settings, include_tree=include_tree)


def focus_hermes() -> bool:
    return _backend().focus_hermes_window()


def show_capture_feedback(label: str) -> None:
    if sys.platform == "win32":
        from . import win32_backend as mod

        mod.show_capture_feedback(label)
