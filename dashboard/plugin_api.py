"""Look At This — authenticated gateway REST routes for Hermes Desktop.

Mounted at ``/api/plugins/look-at-this/`` behind the gateway's own auth.
Delegates to the active ``hermes_plugins.look_at_this`` package when loaded in
the same process so ``HOTKEYS`` and ``STORE`` are never duplicated across
``sys.modules``.
"""

from __future__ import annotations

from pathlib import Path
import sys


def _build_active_router():
    pkg = sys.modules.get("hermes_plugins.look_at_this")
    if pkg is not None:
        try:
            from hermes_plugins.look_at_this.lat.api_handlers import build_router  # type: ignore[import-not-found]

            return build_router()
        except Exception:
            pass
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from lat.api_handlers import build_router

    return build_router()


router = _build_active_router()
