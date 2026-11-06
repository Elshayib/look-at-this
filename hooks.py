"""pre_llm_call injection: one packet, one turn, gone."""

from __future__ import annotations

import logging

from . import lat_core

logger = logging.getLogger(__name__)


def pre_llm_call(session_id: str = "", **kwargs):
    if not session_id:
        return None
    try:
        pkt = lat_core.STORE.consume(session_id)
        if pkt is None:
            # A global-hotkey capture from the companion lands here: it has no
            # session id, so the next turn in any session consumes it.
            pkt = lat_core.take_spooled()
    except Exception:
        logger.exception("look-at-this: packet store failed")
        return None
    if pkt is None:
        return None
    # Keep the PNG until its TTL so vision_analyze can still read it this turn.
    lat_core.STORE.sweep_files(max(60, int(pkt.expires_at - pkt.created_at)))
    return {"context": lat_core.render_context(pkt)}
