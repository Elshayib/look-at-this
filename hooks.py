"""pre_llm_call injection: one packet, one turn, gone."""

from __future__ import annotations

import logging

if __package__:  # normal plugin load: hooks is a module in the plugin package
    from . import lat_core
else:  # direct import for smoke checks, e.g. `python -c "import lat_core, hooks"`
    import lat_core  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


def pre_llm_call(session_id: str = "", **kwargs):
    if not session_id:
        return None
    try:
        pkt = lat_core.STORE.consume(session_id)
        if pkt is None:
            # A global-hotkey capture lives in the companion's memory, bound to
            # the session the Desktop had focused. A remote gateway has no local
            # marker, so the configured backend alone is enough to ask for it.
            settings = lat_core.load_settings()[0]
            if settings.capture_backend == "companion" or lat_core.read_pending_marker() is not None:
                pkt = lat_core.companion_take(settings, session_id)
    except Exception:
        logger.exception("look-at-this: packet store failed")
        return None
    if pkt is None:
        return None
    # Keep the PNG until its TTL so vision_analyze can still read it this turn.
    lat_core.STORE.sweep_files(max(60, int(pkt.expires_at - pkt.created_at)))
    return {"context": lat_core.render_context(pkt)}
