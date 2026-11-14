"""Shared FastAPI router for ``/api/plugins/look-at-this/*``.

Mounted by ``dashboard/plugin_api.py`` (Hermes Desktop's ``plugin_host``
subprocess and ``hermes_cli.web_server``) so the HTTP API and agent hooks
always share one process-wide ``HOTKEYS`` and ``STORE`` state.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

from . import __version__, capture
from .hotkey_daemon import HOTKEYS, parse_hotkey_chord
from .models import (
    ALL_FIELDS,
    CaptureCancelledError,
    LookAtError,
    RegionRect,
    SCHEMA_VERSION,
    _read_yaml_plugin_settings,
    load_live_settings,
    normalize_hotkey_target,
)
from .png_codec import png_bytes_to_data_url
from .render import format_public_packet
from .store import STORE, emit_plugin_event
from .surfaces import set_plugin_config


def _error_response(exc: Exception, status: int = 400) -> JSONResponse:
    if isinstance(exc, CaptureCancelledError):
        return JSONResponse(status_code=200, content={"ok": False, "cancelled": True, "code": "cancelled"})
    if isinstance(exc, LookAtError):
        return JSONResponse(status_code=status, content=exc.to_dict())
    return JSONResponse(
        status_code=500,
        content={"ok": False, "code": "internal_error", "error": f"{type(exc).__name__}: {exc}"},
    )


_HOTKEY_TARGET_ALIASES = frozenset({
    "new_session", "new",
    "ask", "prompt", "choose", "picker",
    "current", "current_session", "active", "active_session", "same",
})
_INT_SETTING_BOUNDS: dict[str, tuple[int, int]] = {
    "ttl_seconds": (30, 3600),
    "max_tree_elements": (20, 1500),
    "max_image_dim": (640, 3840),
}
_BOOL_SETTING_KEYS = frozenset({
    "focus_hermes_on_hotkey",
    "hotkey_hud_feedback",
    "vision_injection",
    "default_redact",
    "include_full_tree_by_default",
})
_LIST_SETTING_KEYS = frozenset({"excluded_apps", "excluded_title_patterns"})
_HOTKEY_SETTING_KEYS = frozenset({"window_hotkey", "snip_hotkey"})


def _validate_and_persist_settings(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if not isinstance(payload, dict):
        raise LookAtError("Settings payload must be a JSON object.", code="invalid_setting")

    validated: dict[str, Any] = {}
    hotkeys_changed = False

    if "hotkey_target" in payload:
        raw_hk = str(payload["hotkey_target"] or "").strip().lower().replace("-", "_").replace(" ", "_")
        if raw_hk not in _HOTKEY_TARGET_ALIASES:
            raise LookAtError(
                f"Invalid hotkey_target: {payload['hotkey_target']!r}. Expected new_session, ask, or current_session.",
                code="invalid_setting",
            )
        validated["hotkey_target"] = normalize_hotkey_target(raw_hk)

    for key in _HOTKEY_SETTING_KEYS:
        if key in payload:
            raw_chord = "" if payload[key] is None else str(payload[key]).strip()
            try:
                parse_hotkey_chord(raw_chord)
            except ValueError as exc:
                raise LookAtError(str(exc), code="invalid_hotkey") from exc
            validated[key] = raw_chord
            hotkeys_changed = True

    for key in _BOOL_SETTING_KEYS:
        if key in payload:
            val = payload[key]
            if isinstance(val, bool):
                validated[key] = val
            elif isinstance(val, str):
                low = val.strip().lower()
                if low in {"1", "true", "yes", "on"}:
                    validated[key] = True
                elif low in {"0", "false", "no", "off"}:
                    validated[key] = False
                else:
                    raise LookAtError(f"Invalid boolean for {key}: {val!r}", code="invalid_setting")
            elif isinstance(val, (int, float)):
                validated[key] = bool(val)
            else:
                raise LookAtError(f"Invalid boolean for {key}: {val!r}", code="invalid_setting")

    for key, (lo, hi) in _INT_SETTING_BOUNDS.items():
        if key in payload:
            try:
                num = int(payload[key])
            except (TypeError, ValueError) as exc:
                raise LookAtError(f"Invalid integer for {key}: {payload[key]!r}", code="invalid_setting") from exc
            validated[key] = max(lo, min(hi, num))

    for key in _LIST_SETTING_KEYS:
        if key in payload:
            raw_list = payload[key]
            if isinstance(raw_list, str):
                items = [line.strip() for line in raw_list.replace(",", "\n").splitlines() if line.strip()]
            elif isinstance(raw_list, (list, tuple)):
                items = [str(x).strip() for x in raw_list if str(x).strip()]
            else:
                raise LookAtError(f"Invalid list for {key}", code="invalid_setting")
            validated[key] = items

    if validated:
        raw_yaml, cfg_mtime = _read_yaml_plugin_settings()
        for key, val in validated.items():
            if set_plugin_config(key, val):
                STORE.clear_preference_override(key)
            else:
                STORE.set_preference_override(
                    key,
                    val,
                    baseline=raw_yaml.get(key),
                    config_mtime=cfg_mtime,
                )
        emit_plugin_event("settings.changed", validated)

    return validated, hotkeys_changed


def _persist_hotkey_target(value: str) -> str:
    updated, _ = _validate_and_persist_settings({"hotkey_target": value})
    return str(updated["hotkey_target"])


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/status")
    def get_status(
        session_id: str = Query("", description="Focused stored session id"),
        preview: int = Query(1, description="Include thumbnail & rendered preview"),
    ) -> Any:
        try:
            cfg = load_live_settings()
            hk_status = HOTKEYS.ensure_started()
            clean_sid = str(session_id or "").strip()
            pkt = STORE.get_pending(clean_sid)
            unbound = STORE.get_unbound_pending() if (pkt is None and clean_sid) else None
            last_hk = STORE.get_last_hotkey_event()
            return {
                "ok": True,
                "version": __version__,
                "schema": SCHEMA_VERSION,
                "platform": sys.platform,
                "pending": format_public_packet(pkt, include_preview=bool(preview)) if pkt else None,
                "unbound_pending": format_public_packet(unbound, include_preview=bool(preview)) if unbound else None,
                "last_hotkey_event": last_hk,
                "hotkeys": hk_status,
                "settings": cfg.to_dict(),
            }
        except Exception as exc:
            return _error_response(exc)

    @router.get("/windows")
    def get_windows(
        thumbnails: int = Query(1, description="Include live window preview thumbnails"),
    ) -> Any:
        try:
            cfg = load_live_settings()
            wins = capture.list_windows(cfg, include_thumbnails=bool(thumbnails))
            return {
                "ok": True,
                "windows": [w.to_dict() for w in wins],
            }
        except Exception as exc:
            return _error_response(exc)

    @router.get("/screenshot")
    def get_full_screenshot(
        session_id: str = Query("", description="Session owning the pending packet"),
        packet_id: str = Query("", description="Optional explicit packet id"),
    ) -> Any:
        try:
            pkt = STORE.get_pending(session_id, packet_id=packet_id)
            if pkt is None:
                pkt = STORE.get_unbound_pending()
            if pkt is None or not pkt.screenshot_path:
                return {"ok": False, "code": "not_found", "error": "No pending screenshot available."}
            raw = Path(pkt.screenshot_path).read_bytes()
            data_url = png_bytes_to_data_url(raw, max_bytes=8 * 1024 * 1024)
            if not data_url:
                return {"ok": False, "code": "too_large", "error": "Screenshot could not be encoded."}
            return {
                "ok": True,
                "id": pkt.id,
                "width": pkt.screenshot_width,
                "height": pkt.screenshot_height,
                "data_url": data_url,
            }
        except Exception as exc:
            return _error_response(exc)

    @router.post("/capture")
    def post_capture(payload: dict[str, Any] = Body(...)) -> Any:
        try:
            cfg = load_live_settings()
            HOTKEYS.ensure_started()
            session_id = str(payload.get("session_id") or "").strip()
            target_mode = payload.get("target_mode")
            if session_id:
                STORE.set_focused_session(session_id)

            mode = str(payload.get("mode") or "active").strip().lower()
            raw_fields = payload.get("fields")
            fields = (
                [str(f) for f in raw_fields if str(f) in ALL_FIELDS]
                if isinstance(raw_fields, list)
                else None
            )
            want_tree = ("tree" in fields) if fields is not None else cfg.include_full_tree_by_default
            redact = payload.get("redact") if isinstance(payload.get("redact"), bool) else None

            if mode == "snip":
                raw = capture.capture_interactive_snip(cfg, include_tree=want_tree)
            elif mode == "region":
                rect = RegionRect.from_dict(payload.get("region"))
                if rect is None:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "ok": False,
                            "code": "invalid_region",
                            "error": "Valid region {x, y, width, height} required.",
                        },
                    )
                raw = capture.capture_region(rect, cfg, include_tree=want_tree)
            else:
                raw = capture.capture_window(
                    cfg,
                    window_id=payload.get("window_id"),
                    hint=str(payload.get("app") or "").strip(),
                    include_screenshot=fields is None or "screenshot" in fields,
                    include_tree=want_tree,
                )

            pkt = STORE.build_and_store(
                raw,
                cfg,
                session_id=session_id,
                fields=fields,
                redact=redact,
                target_mode=str(target_mode) if target_mode is not None else None,
            )
            if mode == "snip" and cfg.focus_hermes_on_hotkey:
                capture.focus_hermes()
            return {
                "ok": True,
                "pending": format_public_packet(pkt, include_preview=True),
            }
        except Exception as exc:
            return _error_response(exc)

    @router.patch("/pending")
    def patch_pending(payload: dict[str, Any] = Body(...)) -> Any:
        try:
            cfg = load_live_settings()
            session_id = str(payload.get("session_id") or "").strip()
            packet_id = str(payload.get("packet_id") or "").strip()
            raw_fields = payload.get("fields")
            fields: list[str] | None = None
            if isinstance(raw_fields, list):
                fields = [str(f) for f in raw_fields if str(f) in ALL_FIELDS]
                if not fields:
                    return JSONResponse(
                        status_code=400,
                        content={"ok": False, "code": "empty_fields", "error": "Keep at least one field enabled."},
                    )

            redact_enabled = payload.get("redact_enabled")
            if redact_enabled is not None and not isinstance(redact_enabled, bool):
                redact_enabled = bool(redact_enabled)

            current = STORE.get_pending(session_id, packet_id=packet_id)
            if current is None and not packet_id:
                current = STORE.get_unbound_pending()
            if current is None:
                return {"ok": True, "pending": None}

            raw_tree: str | None = None
            el_count: int | None = None
            tree_trunc: bool | None = None
            if fields and "tree" in fields and not current.raw_tree_markdown and current.window_id:
                try:
                    refreshed = capture.capture_window(
                        cfg,
                        window_id=current.window_id,
                        include_screenshot=False,
                        include_tree=True,
                    )
                    raw_tree = refreshed.tree_markdown
                    el_count = refreshed.element_count
                    tree_trunc = refreshed.tree_truncated
                except LookAtError:
                    pass

            pkt = STORE.update_pending(
                session_id,
                packet_id=current.id,
                fields=fields,
                redact_enabled=redact_enabled,
                raw_tree_markdown=raw_tree,
                element_count=el_count,
                tree_truncated=tree_trunc,
            )
            return {
                "ok": True,
                "pending": format_public_packet(pkt, include_preview=True) if pkt else None,
            }
        except Exception as exc:
            return _error_response(exc)

    @router.post("/retarget")
    def post_retarget(payload: dict[str, Any] = Body(...)) -> Any:
        try:
            packet_id = str(payload.get("packet_id") or "").strip()
            from_sid = str(payload.get("from_session_id") or "").strip()
            to_sid = str(payload.get("to_session_id") or "").strip()
            target_mode = payload.get("target_mode")
            only_if_new = bool(payload.get("only_if_new_session", False))
            pkt = STORE.retarget_pending(
                packet_id=packet_id,
                from_session_id=from_sid,
                to_session_id=to_sid,
                target_mode=str(target_mode) if target_mode is not None else None,
                only_if_new_session=only_if_new,
            )
            if pkt is None:
                return JSONResponse(
                    status_code=404,
                    content={"ok": False, "code": "not_found", "error": "Capture packet expired or was already used."},
                )
            return {
                "ok": True,
                "pending": format_public_packet(pkt, include_preview=True),
            }
        except Exception as exc:
            return _error_response(exc)

    def _update_settings(payload: dict[str, Any]) -> dict[str, Any]:
        updated, hotkeys_changed = _validate_and_persist_settings(payload)
        if hotkeys_changed:
            HOTKEYS.reload()
        cfg = load_live_settings()
        return {"ok": True, "updated": updated, "settings": cfg.to_dict()}

    @router.post("/preferences")
    def post_preferences(payload: dict[str, Any] = Body(...)) -> Any:
        try:
            return _update_settings(payload)
        except Exception as exc:
            return _error_response(exc)

    @router.get("/settings")
    def get_settings() -> Any:
        try:
            cfg = load_live_settings()
            return {"ok": True, "updated": {}, "settings": cfg.to_dict()}
        except Exception as exc:
            return _error_response(exc)

    @router.post("/settings")
    def post_settings(payload: dict[str, Any] = Body(...)) -> Any:
        try:
            return _update_settings(payload)
        except Exception as exc:
            return _error_response(exc)

    @router.get("/hotkey-seq")
    def get_hotkey_seq() -> Any:
        try:
            last_hk = STORE.get_last_hotkey_event()
            return {
                "ok": True,
                "seq": (last_hk or {}).get("seq", 0),
                "last_hotkey_event": last_hk,
                "hotkeys": HOTKEYS.ensure_started(),
            }
        except Exception as exc:
            return _error_response(exc)

    @router.post("/clear")
    def post_clear(payload: dict[str, Any] = Body(...)) -> Any:
        try:
            session_id = str(payload.get("session_id") or "").strip()
            packet_id = str(payload.get("packet_id") or "").strip()
            cleared = STORE.clear_pending(session_id, packet_id=packet_id)
            return {"ok": True, "cleared": cleared}
        except Exception as exc:
            return _error_response(exc)

    @router.post("/session")
    def post_session(payload: dict[str, Any] = Body(...)) -> Any:
        try:
            session_id = str(payload.get("session_id") or "").strip()
            STORE.set_focused_session(session_id)
            hk = HOTKEYS.reload() if payload.get("reload_hotkeys") else HOTKEYS.ensure_started()
            return {"ok": True, "hotkeys": hk}
        except Exception as exc:
            return _error_response(exc)

    return router
