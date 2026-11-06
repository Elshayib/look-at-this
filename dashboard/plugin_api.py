"""Look At This — gateway-side routes for the Desktop half.

Mounted by Hermes at ``/api/plugins/look-at-this/`` behind the API server's own
authentication. No separate token, no outbound request: the renderer calls
these routes through ``ctx.rest`` and nothing dials out.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

router = APIRouter()

ROOT = Path(__file__).resolve().parents[1]


def _load_core():
    """Reuse the plugin package's module when the agent half is loaded."""
    for name, module in list(sys.modules.items()):
        init = getattr(module, "__file__", None)
        if init and name.startswith("hermes_plugins.") and Path(init).resolve() == (ROOT / "__init__.py"):
            core = sys.modules.get(name + ".lat_core")
            if core is not None:
                return core
    core = sys.modules.get("hermes_look_at_this_core")
    if core is not None:
        return core
    spec = importlib.util.spec_from_file_location("hermes_look_at_this_core", ROOT / "lat_core.py")
    if spec is None or spec.loader is None:
        raise ImportError("look-at-this: cannot load lat_core.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_look_at_this_core"] = module
    spec.loader.exec_module(module)
    return module


CORE = _load_core()


def _settings():
    return CORE.load_settings()[0]


def _bad(exc: Exception) -> JSONResponse:
    if hasattr(exc, "as_dict"):
        return JSONResponse(status_code=400, content=exc.as_dict())
    return JSONResponse(status_code=500, content={"ok": False, "code": "internal_error", "error": str(exc)})


def _session(payload: dict) -> str:
    return str(payload.get("session_id") or "").strip()


def _fields(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if str(v) in CORE.ALL_FIELDS]


@router.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "schema": CORE.SCHEMA,
        "driver": CORE.resolve_driver(),
        "ttl_seconds": _settings().ttl_seconds,
        "platform": sys.platform,
    }


@router.post("/capture")
def capture(payload: dict = Body(...)) -> Any:
    session_id = _session(payload)
    try:
        cfg = _settings()
        hint = str(payload.get("app") or "").strip()
        fields = _fields(payload.get("fields")) or None
        packet, preview = CORE.grab_for_turn(cfg, session_id, hint=hint, fields=fields)
        return {
            "ok": True,
            "packet": packet.public(include_preview=True),
            "preview": preview,
        }
    except Exception as exc:
        return _bad(exc)


@router.get("/pending")
def pending(session_id: str = "", preview: int = 0) -> Any:
    try:
        packet = CORE.pending_packet(session_id)
        pending_obj = packet.public(include_preview=bool(preview)) if packet else None
        return {"ok": True, "pending": pending_obj}
    except Exception as exc:
        return _bad(exc)


@router.patch("/pending")
def update_pending(payload: dict = Body(...)) -> Any:
    try:
        fields = _fields(payload.get("fields"))
        if not fields:
            return JSONResponse(status_code=400, content={
                "ok": False, "code": "no_fields", "error": "fields must name at least one of "
                "screenshot, accessibility, url, selection",
            })
        packet = CORE.update_pending_fields(_session(payload), fields)
        return {"ok": True, "pending": packet.public() if packet else None}
    except Exception as exc:
        return _bad(exc)


@router.post("/clear")
def clear(payload: dict = Body(...)) -> Any:
    try:
        cleared = CORE.clear_pending(_session(payload))
        return {"ok": True, "cleared": cleared}
    except Exception as exc:
        return _bad(exc)


@router.post("/active-session")
def active_session(payload: dict = Body(...)) -> Any:
    try:
        session_id = _session(payload)
        CORE.write_active_session(session_id)
        # A remote gateway writes its own active-session file; relay the id to
        # the companion on the user's machine so a hotkey capture binds there too.
        CORE.companion_set_active_session(_settings(), session_id)
        return {"ok": True}
    except Exception as exc:
        return _bad(exc)
