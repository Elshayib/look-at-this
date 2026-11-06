"""Tool handlers. Settings are rebuilt from ctx.get_config on every call, so a
config edit applies without reloading the plugin."""

from __future__ import annotations

import json
from typing import Any, Callable

from . import lat_core

_GETTER: Callable[..., Any] | None = None


def configure(getter: Callable[..., Any]) -> None:
    global _GETTER
    _GETTER = getter


def settings() -> lat_core.Settings:
    if _GETTER is None:
        return lat_core.Settings()
    return lat_core.Settings.from_getter(_GETTER)


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _fields(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    picked = [str(v) for v in value if str(v) in lat_core.ALL_FIELDS]
    return picked or None


def lat_grab(args: dict, session_id: str = "", **kwargs) -> str:
    """Capture now and return the full packet for this turn (nothing is stored)."""
    cfg = settings()
    fields = _fields(args.get("fields"))
    try:
        cap = lat_core.capture(
            cfg,
            hint=str(args.get("app") or "").strip(),
            include_screenshot=fields is None or "screenshot" in fields,
        )
        pkt = lat_core.make_packet(cap, cfg, session_id or "tool", fields)
        return _json({
            "ok": True,
            "packet": pkt.public(),
            "context": lat_core.render_context(pkt),
            "note": "This packet was returned inline and is not queued for a later turn. "
                    "Use /look or the Desktop hotkey to attach one to the next message.",
        })
    except lat_core.LookAtError as exc:
        return _json(exc.as_dict())
    except Exception as exc:  # never let a capture bug break the turn
        return _json({"ok": False, "code": "internal_error", "error": f"{type(exc).__name__}: {exc}"})


def lat_status(args: dict, session_id: str = "", **kwargs) -> str:
    pkt = lat_core.pending_packet(session_id)
    return _json({
        "ok": True,
        "pending": pkt.public() if pkt else None,
        "ttl_seconds": settings().ttl_seconds,
    })


def lat_clear(args: dict, session_id: str = "", **kwargs) -> str:
    cleared = lat_core.clear_pending(session_id)
    return _json({"ok": True, "cleared": cleared})
