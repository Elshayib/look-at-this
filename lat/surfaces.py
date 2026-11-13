"""Agent surfaces: JSON schemas, tool handlers, lifecycle hooks, vision middleware, and /look."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import time
from typing import Any, Callable
import uuid

from . import capture
from .models import (
    ALL_FIELDS,
    LookAtError,
    Packet,
    RegionRect,
    Settings,
    load_live_settings,
    normalize_fields,
    write_yaml_plugin_setting,
)
from .render import (
    format_cli_preview,
    format_public_packet,
    inject_ephemeral_image_into_request,
    model_accepts_vision,
    render_packet_context,
)
from .store import STORE, safe_unlink_shot

logger = logging.getLogger(__name__)

_CONFIG_GETTER: Callable[..., Any] | None = None
_ASK_CHOICE: Callable[..., Any] | None = None
_CONFIG_SETTER: Callable[..., Any] | None = None


def bind_context(
    config_getter: Callable[..., Any] | None,
    ask_choice: Callable[..., Any] | None = None,
    config_setter: Callable[..., Any] | None = None,
) -> None:
    global _CONFIG_GETTER, _ASK_CHOICE, _CONFIG_SETTER
    _CONFIG_GETTER = config_getter
    _ASK_CHOICE = ask_choice
    _CONFIG_SETTER = config_setter


def set_plugin_config(key: str, value: Any) -> bool:
    wrote = False
    if _CONFIG_SETTER is not None:
        try:
            _CONFIG_SETTER(key, value)
            wrote = True
        except Exception:
            pass
    if write_yaml_plugin_setting(key, value):
        wrote = True
    return wrote


def current_settings() -> Settings:
    return load_live_settings(_CONFIG_GETTER)


# ─────────────────────────────────────────────────────────────────────────────
# Tool JSON Schemas
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA_GRAB: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["window", "region", "snip"],
            "description": (
                "Capture mode: 'window' (default — active/underlying window, or matching app/window_id), "
                "'region' (explicit screen rectangle {x, y, width, height}), or "
                "'snip' (interactive user crosshair overlay to click a window or drag a region)."
            ),
        },
        "app": {
            "type": "string",
            "description": "Optional app name or window-title substring to target when mode='window'.",
        },
        "window_id": {
            "type": ["integer", "string"],
            "description": "Exact window_id from lat_windows to capture.",
        },
        "region": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "width": {"type": "integer", "minimum": 12},
                "height": {"type": "integer", "minimum": 12},
            },
            "required": ["x", "y", "width", "height"],
            "additionalProperties": False,
            "description": "Screen rectangle in pixels when mode='region'.",
        },
        "fields": {
            "type": "array",
            "items": {"type": "string", "enum": list(ALL_FIELDS)},
            "description": (
                "Fields to include: 'screenshot', 'context' (compact focused control + visible UI text), "
                "'tree' (full accessibility tree), 'url', 'selection'."
            ),
        },
        "redact": {
            "type": "boolean",
            "description": "Mask emails, API keys, tokens, card numbers, and sensitive URL params in text.",
        },
        "queue_for_next_turn": {
            "type": "boolean",
            "description": "If true, queue packet for the user's next turn instead of returning inline only.",
        },
    },
    "additionalProperties": False,
}

SCHEMA_WINDOWS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

SCHEMA_STATUS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

SCHEMA_CLEAR: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


# ─────────────────────────────────────────────────────────────────────────────
# Tool Handlers
# ─────────────────────────────────────────────────────────────────────────────

def _json_out(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def tool_lat_windows(args: dict[str, Any], **kwargs: Any) -> str:
    cfg = current_settings()
    try:
        wins = capture.list_windows(cfg, include_thumbnails=False)
        return _json_out({
            "ok": True,
            "count": len(wins),
            "windows": [w.to_dict() for w in wins],
        })
    except LookAtError as exc:
        return _json_out(exc.to_dict())
    except Exception as exc:
        return _json_out({"ok": False, "code": "internal_error", "error": f"{type(exc).__name__}: {exc}"})


def tool_lat_grab(args: dict[str, Any], session_id: str = "", **kwargs: Any) -> str:
    cfg = current_settings()
    mode = str(args.get("mode") or "window").strip().lower()
    fields = normalize_fields(args.get("fields"), include_tree_default=cfg.include_full_tree_by_default)
    want_tree = "tree" in fields
    want_shot = "screenshot" in fields
    redact = cfg.default_redact if args.get("redact") is None else bool(args.get("redact"))
    queue_next = bool(args.get("queue_for_next_turn", False))

    try:
        if mode == "snip":
            raw = capture.capture_interactive_snip(cfg, include_tree=want_tree)
        elif mode == "region":
            rect = RegionRect.from_dict(args.get("region"))
            if rect is None:
                return _json_out({
                    "ok": False,
                    "code": "invalid_region",
                    "error": "mode='region' requires region={x, y, width, height} with positive dimensions.",
                })
            raw = capture.capture_region(rect, cfg, include_tree=want_tree)
        else:
            raw = capture.capture_window(
                cfg,
                window_id=args.get("window_id"),
                hint=str(args.get("app") or "").strip(),
                include_screenshot=want_shot,
                include_tree=want_tree,
            )

        if queue_next:
            pkt = STORE.build_and_store(raw, cfg, session_id=session_id, fields=fields, redact=redact)
            return _json_out({
                "ok": True,
                "queued": True,
                "packet": format_public_packet(pkt, include_preview=False),
            })

        now = time.time()
        pkt = Packet(
            id=raw.capture_id or uuid.uuid4().hex[:12],
            session_id=session_id or "inline",
            created_at=now,
            expires_at=now + cfg.ttl_seconds,
            mode=raw.mode,
            app=raw.app,
            pid=raw.pid,
            window_id=raw.window_id,
            bounds=raw.bounds,
            raw_title=raw.title,
            raw_url=raw.url,
            raw_selection=raw.selection,
            raw_context_text=raw.context_text,
            raw_tree_markdown=raw.tree_markdown,
            element_count=raw.element_count,
            tree_truncated=raw.tree_truncated,
            screenshot_path=raw.screenshot_path if want_shot else None,
            screenshot_width=raw.screenshot_width if want_shot else None,
            screenshot_height=raw.screenshot_height if want_shot else None,
            thumbnail_data_url=None,
            fields=fields,
            redact_enabled=redact,
        )
        if not want_shot and raw.screenshot_path:
            safe_unlink_shot(raw.screenshot_path)
        elif want_shot and pkt.screenshot_path:
            # Stage for turn clean-up so inline tool PNGs are also auto-deleted at turn end!
            STORE.stage_inline_shot(session_id or "inline", pkt)

        return _json_out({
            "ok": True,
            "queued": False,
            "packet": format_public_packet(pkt, include_preview=False),
            "screenshot_path": pkt.screenshot_path,
            "context": render_packet_context(pkt, vision_attached=False),
            "hint": (
                f"Call vision_analyze on {pkt.screenshot_path!r} if you need to inspect visual layout, "
                "charts, colors, or rendered graphics."
                if pkt.screenshot_path
                else ""
            ),
        })
    except LookAtError as exc:
        return _json_out(exc.to_dict())
    except Exception as exc:
        return _json_out({"ok": False, "code": "internal_error", "error": f"{type(exc).__name__}: {exc}"})


def tool_lat_status(args: dict[str, Any], session_id: str = "", **kwargs: Any) -> str:
    pkt = STORE.get_pending(session_id)
    unbound = STORE.get_unbound_pending() if pkt is None and session_id else None
    return _json_out({
        "ok": True,
        "pending": format_public_packet(pkt, include_preview=False) if pkt else None,
        "unbound_pending": format_public_packet(unbound, include_preview=False) if unbound else None,
        "ttl_seconds": current_settings().ttl_seconds,
    })


def tool_lat_clear(args: dict[str, Any], session_id: str = "", **kwargs: Any) -> str:
    cleared = STORE.clear_pending(session_id)
    return _json_out({"ok": True, "cleared": cleared})


# ─────────────────────────────────────────────────────────────────────────────
# Hooks & Ephemeral Vision Middleware
# ─────────────────────────────────────────────────────────────────────────────

def hook_pre_llm_call(session_id: str = "", model: str = "", **kwargs: Any) -> dict[str, str] | None:
    if not session_id:
        return None
    try:
        # Subagents and automated background tasks must NEVER consume user screen captures
        if kwargs.get("parent_session_id"):
            return None
        platform = str(kwargs.get("platform") or "").strip().lower()
        if platform in {"cron", "webhook"}:
            return None
        user_msg = kwargs.get("user_message")
        if isinstance(user_msg, str) and user_msg.lstrip().startswith(
            ("[ASYNC DELIVERY", "[WATCH", "[HEARTBEAT")
        ):
            return None

        is_first_turn = bool(kwargs.get("is_first_turn", True))
        pkt = STORE.consume_for_turn(
            session_id,
            is_first_turn=is_first_turn,
            platform=platform,
        )
        if pkt is None:
            return None
        cfg = current_settings()
        has_shot = bool("screenshot" in pkt.fields and pkt.screenshot_path and Path(pkt.screenshot_path).is_file())
        vision_will_attach = bool(
            has_shot and cfg.vision_injection and model_accepts_vision(kwargs.get("provider"), model)
        )
        logger.info(
            "look-at-this: attached capture %s (%s) to session %s platform=%s first_turn=%s vision=%s",
            pkt.id, pkt.target_mode, session_id, platform or "?", is_first_turn, vision_will_attach,
        )
        return {"context": render_packet_context(pkt, vision_attached=vision_will_attach)}
    except Exception:
        logger.exception("look-at-this: pre_llm_call failed")
        return None


def middleware_llm_request(
    request: dict[str, Any],
    session_id: str = "",
    provider: str = "",
    model: str = "",
    api_mode: str = "",
    **kwargs: Any,
) -> dict[str, Any] | None:
    if not session_id:
        return None
    try:
        pkt = STORE.active_turn_packet(session_id)
        if pkt is None or "screenshot" not in pkt.fields or not pkt.screenshot_path:
            return None
        cfg = current_settings()
        if not cfg.vision_injection:
            return None
        return inject_ephemeral_image_into_request(
            request,
            screenshot_path=pkt.screenshot_path,
            api_mode=api_mode,
            provider=provider,
            model=model,
        )
    except Exception:
        logger.exception("look-at-this: llm_request middleware failed")
        return None


def hook_post_llm_call(session_id: str = "", **kwargs: Any) -> None:
    if session_id:
        STORE.finish_turn(session_id)
    STORE.finish_turn("inline")


def hook_on_session_end(session_id: str = "", **kwargs: Any) -> None:
    if session_id:
        STORE.finish_turn(session_id)
    STORE.finish_turn("inline")
    STORE.sweep_expired()


# ─────────────────────────────────────────────────────────────────────────────
# /look Slash Command
# ─────────────────────────────────────────────────────────────────────────────

LOOK_HELP = """\
Look At This v2 — Ephemeral Screen & Window Context:
  /look                  Capture the active/underlying window for your next message
  /look pick             Pick from open visible windows interactively
  /look snip             Open interactive crosshair overlay (click window or drag region)
  /look app=<query>      Capture window matching app name or title
  /look redact [on|off]  Toggle sensitive-text redaction on the queued packet
  /look tree [on|off]    Toggle full accessibility tree on the queued packet
  /look status           Inspect the currently queued packet
  /look clear            Discard the queued packet and delete its screenshot
  /look help             Show this help"""


def handle_look_command(raw_args: str, session_id: str = "", platform: str = "", **kwargs: Any) -> str:
    if session_id:
        STORE.set_focused_session(session_id)
    cfg = current_settings()
    tokens = [t for t in (raw_args or "").strip().split() if t]
    first = tokens[0].lower() if tokens else ""

    if first == "help":
        return LOOK_HELP

    if first == "status":
        pkt = STORE.get_pending(session_id)
        if pkt is None:
            return "Look At This: no packet queued. Run `/look`, `/look pick`, or `/look snip`."
        return format_cli_preview(pkt)

    if first == "clear":
        cleared = STORE.clear_pending(session_id)
        return "Look At This: pending packet discarded." if cleared else "Look At This: nothing queued."

    if first == "setfields" and len(tokens) > 1:
        pkt = STORE.get_pending(session_id)
        if pkt is None:
            return "Look At This: no packet queued."
        req_fields = [v.strip().lower() for v in tokens[1].split(",") if v.strip()]
        next_fields = normalize_fields(req_fields)
        raw_tree: str | None = None
        el_count: int | None = None
        tree_trunc: bool | None = None
        if "tree" in next_fields and not pkt.raw_tree_markdown and pkt.window_id:
            try:
                raw = capture.capture_window(
                    cfg,
                    window_id=pkt.window_id,
                    include_screenshot=False,
                    include_tree=True,
                )
                raw_tree = raw.tree_markdown
                el_count = raw.element_count
                tree_trunc = raw.tree_truncated
            except LookAtError as exc:
                return f"Look At This: {exc}"
        updated = STORE.update_pending(
            session_id,
            packet_id=pkt.id,
            fields=next_fields,
            raw_tree_markdown=raw_tree,
            element_count=el_count,
            tree_truncated=tree_trunc,
        )
        return format_cli_preview(updated) if updated else "Look At This: packet expired."

    if first == "redact":
        pkt = STORE.get_pending(session_id)
        if pkt is None:
            return "Look At This: no packet queued to toggle redaction on."
        arg = tokens[1].lower() if len(tokens) > 1 else ""
        next_val = (arg in {"on", "1", "true", "yes"}) if arg else (not pkt.redact_enabled)
        updated = STORE.update_pending(session_id, packet_id=pkt.id, redact_enabled=next_val)
        return format_cli_preview(updated) if updated else "Look At This: packet expired."

    if first == "tree":
        pkt = STORE.get_pending(session_id)
        if pkt is None:
            return "Look At This: no packet queued to toggle tree on."
        arg = tokens[1].lower() if len(tokens) > 1 else ""
        has_tree = "tree" in pkt.fields
        enable_tree = (arg in {"on", "1", "true", "yes"}) if arg else (not has_tree)
        raw_tree: str | None = None
        el_count: int | None = None
        tree_trunc: bool | None = None
        if enable_tree and not pkt.raw_tree_markdown and pkt.window_id:
            try:
                raw = capture.capture_window(
                    cfg,
                    window_id=pkt.window_id,
                    include_screenshot=False,
                    include_tree=True,
                )
                raw_tree = raw.tree_markdown
                el_count = raw.element_count
                tree_trunc = raw.tree_truncated
            except LookAtError as exc:
                return f"Look At This: {exc}"
        next_fields = (
            ([*pkt.fields, "tree"] if "tree" not in pkt.fields else list(pkt.fields))
            if enable_tree
            else [f for f in pkt.fields if f != "tree"]
        )
        if not next_fields:
            next_fields = ["context"]
        updated = STORE.update_pending(
            session_id,
            packet_id=pkt.id,
            fields=next_fields,
            raw_tree_markdown=raw_tree,
            element_count=el_count,
            tree_truncated=tree_trunc,
        )
        return format_cli_preview(updated) if updated else "Look At This: packet expired."

    try:
        if first == "snip":
            raw = capture.capture_interactive_snip(cfg, include_tree=cfg.include_full_tree_by_default)
            pkt = STORE.build_and_store(raw, cfg, session_id=session_id)
            return format_cli_preview(pkt)

        if first == "pick":
            wins = [w for w in capture.list_windows(cfg) if not w.is_excluded]
            if not wins:
                return "Look At This: no capturable windows are open right now."
            if _ASK_CHOICE is None:
                listing = "\n".join(f"  • {w.app_display} — {w.title}" for w in wins[:10])
                return f"Open windows (use `/look app=<name>`):\n{listing}"
            choices = [
                {
                    "value": str(w.window_id),
                    "label": f"{w.app_display} — {w.title[:56]}",
                    "description": f"{w.bounds.width}×{w.bounds.height}",
                }
                for w in wins[:12]
            ]
            picked = _ASK_CHOICE("Capture which window?", choices)
            if not picked:
                return "Look At This: window selection cancelled."
            raw = capture.capture_window(
                cfg, window_id=picked, include_screenshot=True, include_tree=cfg.include_full_tree_by_default
            )
            pkt = STORE.build_and_store(raw, cfg, session_id=session_id)
            return format_cli_preview(pkt)

        # Parse optional app=<hint> fields=a,b redact=on
        hint = ""
        fields_override: list[str] | None = None
        redact_override: bool | None = None
        for tok in tokens:
            low = tok.lower()
            if low.startswith(("app=", "window=")):
                hint = tok.split("=", 1)[1].strip()
            elif low.startswith("fields="):
                vals = [v.strip().lower() for v in tok.split("=", 1)[1].split(",") if v.strip()]
                fields_override = [v for v in vals if v in ALL_FIELDS] or None
            elif low.startswith("redact="):
                redact_override = low.split("=", 1)[1] in {"1", "true", "on", "yes"}
            elif not hint:
                hint = tok

        eff_fields = normalize_fields(fields_override, include_tree_default=cfg.include_full_tree_by_default)
        raw = capture.capture_window(
            cfg,
            hint=hint,
            include_screenshot="screenshot" in eff_fields,
            include_tree="tree" in eff_fields,
        )
        pkt = STORE.build_and_store(
            raw, cfg, session_id=session_id, fields=eff_fields, redact=redact_override
        )
        return format_cli_preview(pkt)
    except LookAtError as exc:
        detail = f"\n{exc.detail}" if exc.detail else ""
        return f"Look At This: {exc}{detail}"
    except Exception as exc:
        logger.exception("look-at-this /look failed")
        return f"Look At This error: {type(exc).__name__}: {exc}"
