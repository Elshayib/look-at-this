"""Look At This — user-initiated, one-shot screen context for the next turn."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import hooks, lat_core, schemas, tools

logger = logging.getLogger(__name__)

# The gateway's web server imports dashboard/plugin_api.py by path. Register the
# already-loaded core module so both halves share one in-memory packet store.
sys.modules.setdefault("hermes_look_at_this_core", lat_core)

_HELP = """\
/look                 capture the current window and attach it to your next message
/look app=chrome.exe  capture a specific app or window title instead
/look fields=url,selection
                      capture with only these fields (screenshot, accessibility, url, selection)
/look status          what is pending, and for how long
/look clear           discard the pending packet and delete its screenshot
/look help            this text"""


def _parse(raw: str) -> dict:
    out: dict = {"cmd": "grab", "app": "", "fields": None}
    for token in raw.replace(",", " ").split():
        low = token.lower()
        if low in ("status", "clear", "help"):
            out["cmd"] = low
        elif low.startswith("app=") or low.startswith("window="):
            out["app"] = token.split("=", 1)[1]
        elif low.startswith("fields="):
            values = [v for v in token.split("=", 1)[1].replace(",", " ").split() if v]
            out["fields"] = [v for v in values if v in lat_core.ALL_FIELDS] or None
    return out


def _look(raw_args: str, session_id: str = "", platform: str = "", **kwargs) -> str:
    parsed = _parse(raw_args or "")
    cfg = tools.settings()

    if parsed["cmd"] == "help":
        return _HELP

    if parsed["cmd"] == "status":
        pkt = lat_core.pending_packet(session_id)
        if pkt is None:
            return "Look At This: nothing pending. /look captures the current window."
        return lat_core.preview_text(pkt)

    if parsed["cmd"] == "clear":
        cleared = lat_core.clear_pending(session_id)
        return "Look At This: pending packet discarded." if cleared else "Look At This: nothing to discard."

    try:
        _pkt, preview = lat_core.grab_for_turn(
            cfg, session_id, hint=parsed["app"], fields=parsed["fields"]
        )
        where = f" from {platform}" if platform else ""
        return f"Look At This{where}:\n{preview}"
    except lat_core.LookAtError as exc:
        logger.info("look-at-this command refused: %s", exc)
        detail = f"\n{exc.detail}" if exc.detail else ""
        return f"Look At This: {exc}{detail}"


def register(ctx) -> None:
    tools.configure(ctx.get_config)

    ctx.register_tool(
        name="lat_grab",
        toolset="look_at_this",
        schema=schemas.GRAB,
        handler=tools.lat_grab,
        description="Capture the user's current window (screenshot + accessibility tree + URL + selection) right now.",
    )
    ctx.register_tool(
        name="lat_status",
        toolset="look_at_this",
        schema=schemas.STATUS,
        handler=tools.lat_status,
        description="Report the Look At This packet pending for this session, if any.",
    )
    ctx.register_tool(
        name="lat_clear",
        toolset="look_at_this",
        schema=schemas.CLEAR,
        handler=tools.lat_clear,
        description="Discard the pending Look At This packet and delete its screenshot.",
    )

    ctx.register_hook("pre_llm_call", hooks.pre_llm_call)
    ctx.register_command(
        "look",
        _look,
        description="Capture the current window as one ephemeral context packet for your next message",
        args_hint="[app=<name>] [fields=a,b] | status | clear | help",
    )

    skills_dir = Path(__file__).parent / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                try:
                    ctx.register_skill(child.name, skill_md)
                except Exception:
                    logger.exception("look-at-this: could not register skill %s", child.name)

    logger.info("look-at-this registered (driver=%s)", lat_core.resolve_driver() or "missing")
