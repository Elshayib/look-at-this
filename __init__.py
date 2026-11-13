"""Look At This — seamless ephemeral screen, window, and region context."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .lat import api_handlers, hotkey_daemon, surfaces
from .lat.capture import uia_worker

__version__ = "0.2.1"

logger = logging.getLogger(__name__)


def _refresh_mounted_web_routes() -> None:
    """Hot-swap ``/api/plugins/look-at-this`` routes if ``web_server`` is already live."""
    web_mod = sys.modules.get("hermes_cli.web_server")
    app = getattr(web_mod, "app", None)
    if app is None or not hasattr(app, "router"):
        return
    try:
        prefix = "/api/plugins/look-at-this"
        routes = app.router.routes
        deps = None
        first_idx: int | None = None
        kept = []
        for idx, route in enumerate(routes):
            rpath = str(getattr(route, "path", "") or "")
            if rpath == prefix or rpath.startswith(prefix + "/"):
                if first_idx is None:
                    first_idx = idx
                    deps = list(getattr(route, "dependencies", None) or [])
                continue
            kept.append(route)
        if first_idx is None:
            # Not mounted yet — web_server_dashboard hot-mount will pick up plugin_api.py on first request
            return
        before_len = len(routes)
        app.include_router(api_handlers.build_router(), prefix=prefix, dependencies=deps or [])
        fresh = list(routes[before_len:])
        routes[:] = kept[:first_idx] + fresh + kept[first_idx:]
        logger.info("look-at-this: refreshed %d live API routes on web_server", len(fresh))
    except Exception:
        logger.debug("look-at-this: live API route refresh skipped", exc_info=True)


def register(ctx) -> None:
    surfaces.bind_context(
        getattr(ctx, "get_config", None),
        getattr(ctx, "ask_choice", None),
        getattr(ctx, "set_config", None),
    )

    ctx.register_tool(
        name="lat_grab",
        toolset="look_at_this",
        schema=surfaces.SCHEMA_GRAB,
        handler=surfaces.tool_lat_grab,
        description=(
            "Capture the user's active window, a specific open window, or a screen region "
            "right now (screenshot + smart UI context + URL + selection)."
        ),
    )
    ctx.register_tool(
        name="lat_windows",
        toolset="look_at_this",
        schema=surfaces.SCHEMA_WINDOWS,
        handler=surfaces.tool_lat_windows,
        description="List currently visible application windows available for Look At This capture.",
    )
    ctx.register_tool(
        name="lat_status",
        toolset="look_at_this",
        schema=surfaces.SCHEMA_STATUS,
        handler=surfaces.tool_lat_status,
        description="Check if a Look At This capture packet is currently queued for this session.",
    )
    ctx.register_tool(
        name="lat_clear",
        toolset="look_at_this",
        schema=surfaces.SCHEMA_CLEAR,
        handler=surfaces.tool_lat_clear,
        description="Discard any pending Look At This packet for this session and delete its screenshot.",
    )

    ctx.register_hook("pre_llm_call", surfaces.hook_pre_llm_call)
    ctx.register_hook("post_llm_call", surfaces.hook_post_llm_call)
    ctx.register_hook("on_session_end", surfaces.hook_on_session_end)
    ctx.register_middleware("llm_request", surfaces.middleware_llm_request)

    ctx.register_command(
        "look",
        surfaces.handle_look_command,
        description="Capture a window or screen region as an ephemeral context packet for your next message",
        args_hint="[pick | snip | app=<name> | redact | tree | status | clear | version | help]",
    )

    skills_dir = Path(__file__).parent / "skills"
    if skills_dir.is_dir() and hasattr(ctx, "register_skill"):
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                try:
                    ctx.register_skill(child.name, skill_md)
                except Exception:
                    logger.exception("look-at-this: failed to register skill %s", child.name)

    # Start OS global hotkeys automatically inside the live Hermes gateway/server,
    # while staying completely inert during CLI validation, doctor, and one-shot commands.
    if "gateway.run" in sys.modules or "hermes_cli.web_server" in sys.modules:
        # Stop any legacy standalone `lat.hotkey_daemon` instance if an older import created one
        legacy_hk_mod = sys.modules.get("lat.hotkey_daemon")
        if legacy_hk_mod is not None and getattr(legacy_hk_mod, "HOTKEYS", None) is not hotkey_daemon.HOTKEYS:
            try:
                legacy_hk_mod.HOTKEYS.stop()
            except Exception:
                pass
        _refresh_mounted_web_routes()
        hk = hotkey_daemon.HOTKEYS.reload()
        if hasattr(ctx, "on_unload"):
            ctx.on_unload(hotkey_daemon.HOTKEYS.stop)
            ctx.on_unload(uia_worker.UIA.close)
        logger.info("look-at-this global hotkeys initialized: %s", hk)
