"""Unified cross-process ephemeral packet store + live Desktop event broadcasting.

Uses Hermes's sanctioned ``plugins.plugin_storage`` SQLite WAL database so the
agent hooks, ``/look`` slash command, ``dashboard/plugin_api.py`` routes, and
global hotkey thread share one race-free source of truth without ``sys.modules``
hacks or HTTP polling.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from typing import Any, Iterator

from .models import (
    Packet,
    RawCapture,
    Settings,
    UNBOUND_SESSION,
    normalize_fields,
    normalize_hotkey_target,
    normalize_target_mode,
)

logger = logging.getLogger(__name__)

PLUGIN_ID = "look-at-this"

# Unbound target modes that an already-existing chat must never pick up implicitly.
RESERVED_UNBOUND_MODES = frozenset({"new_session", "ask"})

# Remote messaging / automation platforms: a desktop screen capture queued for
# "the next new session" must never be swallowed by a stranger's first message.
REMOTE_PLATFORMS = frozenset({
    "api_server", "bluebubbles", "cron", "dingtalk", "discord", "email", "feishu",
    "homeassistant", "lark", "line", "matrix", "mattermost", "qq", "qqbot", "signal",
    "slack", "sms", "teams", "telegram", "webhook", "wecom", "weixin", "whatsapp",
})

# Clock-skew allowance when comparing a session's start time to a capture time.
_SESSION_START_SKEW_SECONDS = 2.0


def lookup_session_started_at(session_id: str) -> float | None:
    """Return ``sessions.started_at`` for *session_id* from Hermes ``state.db``.

    Read-only, best effort: returns ``None`` when the row or database is
    unavailable so callers fall back to Hermes's own ``is_first_turn`` signal.
    """
    clean = str(session_id or "").strip()
    if not clean:
        return None
    try:
        try:
            from hermes_constants import get_hermes_home

            home = Path(get_hermes_home())
        except Exception:
            env = os.environ.get("HERMES_HOME", "").strip()
            home = Path(env).expanduser() if env else (Path.home() / ".hermes")
        db = home / "state.db"
        if not db.is_file():
            return None
        uri = "file:" + db.resolve().as_posix() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as conn:
            row = conn.execute(
                "SELECT started_at FROM sessions WHERE id = ?", (clean,)
            ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
    except Exception:
        logger.debug("look-at-this: session start lookup failed", exc_info=True)
    return None


def unbound_claim_allowed(
    pkt: Packet,
    session_id: str,
    *,
    is_first_turn: bool,
    platform: str = "",
    started_at: float | None = None,
) -> bool:
    """Decide whether *session_id* may implicitly consume an unbound packet.

    - ``ask``: never — the user must pick a destination first (retarget).
    - ``new_session``: only the first turn of a local session that did not
      already exist before the capture was taken.
    - anything else (``any``): legacy behaviour, first come first served.
    """
    mode = pkt.target_mode
    if mode == "ask":
        return False
    if mode != "new_session":
        return True
    if not is_first_turn:
        return False
    if str(platform or "").strip().lower() in REMOTE_PLATFORMS:
        return False
    if started_at is not None and started_at < pkt.created_at - _SESSION_START_SKEW_SECONDS:
        return False
    return True


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pending_packets (
    slot_key TEXT PRIMARY KEY,
    packet_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS active_turns (
    session_id TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


def get_data_dir() -> Path:
    """Resolve ``<HERMES_HOME>/plugin-data/look-at-this`` via Hermes public API."""
    try:
        from plugins.plugin_storage import plugin_data_dir

        d = Path(plugin_data_dir(PLUGIN_ID))
    except Exception:
        try:
            from hermes_constants import get_hermes_home

            base = Path(get_hermes_home())
        except Exception:
            env = os.environ.get("HERMES_HOME", "").strip()
            base = Path(env).expanduser() if env else (Path.home() / ".hermes")
        d = base / "plugin-data" / PLUGIN_ID
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def get_shots_dir() -> Path:
    shots = get_data_dir() / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    return shots


def safe_unlink_shot(path_str: str | None) -> None:
    """Delete a screenshot PNG only if it resides inside our own shots/ folder."""
    if not path_str:
        return
    try:
        target = Path(path_str).resolve()
        target.relative_to(get_shots_dir().resolve())
        if target.suffix.lower() == ".png":
            target.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def emit_plugin_event(event: str, payload: dict[str, Any] | None = None) -> None:
    """Push a real-time event to connected Hermes Desktop windows via public bridge."""
    if "unittest" in sys.modules:
        return
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(PLUGIN_ID, event, payload or {})
    except Exception:
        pass


_INITIALIZED_DBS: set[str] = set()


class EphemeralStore:
    """Thread-safe, cross-process store backed by SQLite WAL."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._explicit_db_path = db_path
        self._lock = threading.RLock()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        path = self._explicit_db_path if self._explicit_db_path is not None else (get_data_dir() / "data.db")
        path.parent.mkdir(parents=True, exist_ok=True)
        resolved = str(path.resolve())
        needs_init = (not path.exists()) or (resolved not in _INITIALIZED_DBS)
        raw_conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
        with closing(raw_conn) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=3000")
            if needs_init:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(_SCHEMA_SQL)
                _INITIALIZED_DBS.add(resolved)
            yield conn

    @staticmethod
    def _clear_last_hotkey_if_matches(conn: sqlite3.Connection, packet_id: str) -> None:
        clean_pid = str(packet_id or "").strip()
        if not clean_pid:
            return
        row = conn.execute(
            "SELECT value FROM runtime_state WHERE key = ?",
            ("last_hotkey_event",),
        ).fetchone()
        if not row:
            return
        try:
            evt = json.loads(str(row[0] or ""))
        except Exception:
            conn.execute("DELETE FROM runtime_state WHERE key = ?", ("last_hotkey_event",))
            return
        if isinstance(evt, dict) and str(evt.get("packet_id") or "") == clean_pid:
            conn.execute("DELETE FROM runtime_state WHERE key = ?", ("last_hotkey_event",))

    @staticmethod
    def _collect_turn_shots_from_json(raw_json: str) -> list[str]:
        shots: list[str] = []
        try:
            data = json.loads(raw_json)
        except Exception:
            return shots
        if not isinstance(data, dict):
            return shots
        main_shot = data.get("screenshot_path")
        if isinstance(main_shot, str) and main_shot.strip():
            shots.append(main_shot.strip())
        extra = data.get("extra_screenshot_paths")
        if isinstance(extra, list):
            for p in extra:
                if isinstance(p, str) and p.strip():
                    shots.append(p.strip())
        return shots

    @classmethod
    def _write_active_turn_payload(
        cls,
        conn: sqlite3.Connection,
        session_id: str,
        deadline: float,
        pkt_dict: dict[str, Any],
    ) -> None:
        existing_row = conn.execute(
            "SELECT payload_json FROM active_turns WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        prior_shots: list[str] = []
        if existing_row:
            prior_shots = cls._collect_turn_shots_from_json(str(existing_row[0] or ""))
        current_main = str(pkt_dict.get("screenshot_path") or "").strip()
        seen: set[str] = set()
        if current_main:
            seen.add(current_main)
        merged_extra: list[str] = []
        for s in prior_shots:
            if s and s not in seen:
                seen.add(s)
                merged_extra.append(s)
        if merged_extra:
            pkt_dict["extra_screenshot_paths"] = merged_extra
        conn.execute(
            "INSERT INTO active_turns(session_id, expires_at, payload_json) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET expires_at=excluded.expires_at, payload_json=excluded.payload_json",
            (session_id, deadline, json.dumps(pkt_dict, ensure_ascii=False)),
        )

    @staticmethod
    def _slot(session_id: str) -> str:
        clean = str(session_id or "").strip()
        return clean if clean else UNBOUND_SESSION

    def set_preference_override(
        self,
        key: str,
        value: Any,
        *,
        baseline: Any = None,
        config_mtime: float = 0.0,
    ) -> None:
        record = {
            "v": 2,
            "value": value,
            "baseline": baseline,
            "config_mtime": float(config_mtime or 0.0),
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO runtime_state(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (f"pref:{key}", json.dumps(record, ensure_ascii=False), time.time()),
            )

    def clear_preference_override(self, key: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM runtime_state WHERE key = ?", (f"pref:{key}",))

    def get_preference_entry(self, key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value, updated_at FROM runtime_state WHERE key = ?",
                (f"pref:{key}",),
            ).fetchone()
            if not row or row[0] is None:
                return None
            raw = str(row[0])
            updated_at = float(row[1] or 0.0)
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed.get("v") == 2 and "value" in parsed:
                    return {
                        "value": parsed.get("value"),
                        "baseline": parsed.get("baseline"),
                        "config_mtime": float(parsed.get("config_mtime") or 0.0),
                        "updated_at": updated_at,
                        "legacy": False,
                    }
            except Exception:
                pass
            return {
                "value": raw,
                "baseline": None,
                "config_mtime": 0.0,
                "updated_at": updated_at,
                "legacy": True,
            }

    def get_preference_override(self, key: str) -> str | None:
        entry = self.get_preference_entry(key)
        if not entry:
            return None
        val = entry.get("value")
        return str(val) if val is not None else None

    def set_focused_session(self, session_id: str) -> None:
        clean = str(session_id or "").strip()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO runtime_state(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                ("focused_session_id", clean, time.time()),
            )

    def get_focused_session(self) -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value, updated_at FROM runtime_state WHERE key = ?",
                ("focused_session_id",),
            ).fetchone()
            if not row:
                return ""
            val, updated_at = str(row[0] or ""), float(row[1] or 0)
            # Stale after 12 hours
            if time.time() - updated_at > 43200:
                return ""
            return val.strip()

    def build_and_store(
        self,
        cap: RawCapture,
        settings: Settings,
        session_id: str = "",
        *,
        fields: list[str] | None = None,
        redact: bool | None = None,
        target_mode: str | None = None,
    ) -> Packet:
        now = time.time()
        clean_sid = str(session_id or "").strip()
        effective_fields = normalize_fields(
            fields, include_tree_default=settings.include_full_tree_by_default
        )
        pkt = Packet(
            id=cap.capture_id,
            session_id=clean_sid,
            created_at=now,
            expires_at=now + settings.ttl_seconds,
            mode=cap.mode,
            app=cap.app,
            pid=cap.pid,
            window_id=cap.window_id,
            bounds=cap.bounds,
            raw_title=cap.title,
            raw_url=cap.url,
            raw_selection=cap.selection,
            raw_context_text=cap.context_text,
            raw_tree_markdown=cap.tree_markdown,
            element_count=cap.element_count,
            tree_truncated=cap.tree_truncated,
            screenshot_path=cap.screenshot_path,
            screenshot_width=cap.screenshot_width,
            screenshot_height=cap.screenshot_height,
            thumbnail_data_url=cap.thumbnail_data_url,
            fields=effective_fields,
            redact_enabled=settings.default_redact if redact is None else bool(redact),
            target_mode=normalize_target_mode(target_mode, has_session=bool(clean_sid)),
        )
        return self.put(pkt)

    def put(self, pkt: Packet) -> Packet:
        self.sweep_expired()
        slot = self._slot(pkt.session_id)
        old_shot: str | None = None
        payload = json.dumps(pkt.to_storage_dict(), ensure_ascii=False)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM pending_packets WHERE slot_key = ?", (slot,)
            ).fetchone()
            if row:
                try:
                    prev = Packet.from_storage_dict(json.loads(row[0]))
                    if prev.screenshot_path and prev.screenshot_path != pkt.screenshot_path:
                        old_shot = prev.screenshot_path
                except Exception:
                    pass
            conn.execute(
                "INSERT INTO pending_packets(slot_key, packet_id, session_id, created_at, expires_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(slot_key) DO UPDATE SET "
                "packet_id=excluded.packet_id, session_id=excluded.session_id, "
                "created_at=excluded.created_at, expires_at=excluded.expires_at, payload_json=excluded.payload_json",
                (slot, pkt.id, pkt.session_id, pkt.created_at, pkt.expires_at, payload),
            )
        if old_shot:
            safe_unlink_shot(old_shot)
        from .render import format_public_packet

        emit_plugin_event(
            "state.changed",
            {
                "action": "captured",
                "session_id": pkt.session_id,
                "packet_id": pkt.id,
                "pending": format_public_packet(pkt, include_preview=True),
            },
        )
        return pkt

    def _resolve_row(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        allow_new_session_unbound: bool = False,
        packet_id: str = "",
    ) -> tuple[str, Packet] | None:
        now = time.time()
        clean_pid = str(packet_id or "").strip()
        if clean_pid:
            rows = conn.execute(
                "SELECT slot_key, payload_json FROM pending_packets WHERE packet_id = ?",
                (clean_pid,),
            ).fetchall()
            for key, payload_json in rows:
                try:
                    pkt = Packet.from_storage_dict(json.loads(payload_json))
                except Exception:
                    conn.execute("DELETE FROM pending_packets WHERE slot_key = ?", (key,))
                    continue
                if pkt.is_expired(now):
                    conn.execute("DELETE FROM pending_packets WHERE slot_key = ?", (key,))
                    safe_unlink_shot(pkt.screenshot_path)
                    continue
                return str(key), pkt
            return None

        slot = self._slot(session_id)
        candidates = [slot] if slot == UNBOUND_SESSION else [slot, UNBOUND_SESSION]
        for key in candidates:
            row = conn.execute(
                "SELECT payload_json FROM pending_packets WHERE slot_key = ?", (key,)
            ).fetchone()
            if not row:
                continue
            try:
                pkt = Packet.from_storage_dict(json.loads(row[0]))
            except Exception:
                conn.execute("DELETE FROM pending_packets WHERE slot_key = ?", (key,))
                continue
            if pkt.is_expired(now):
                conn.execute("DELETE FROM pending_packets WHERE slot_key = ?", (key,))
                safe_unlink_shot(pkt.screenshot_path)
                continue
            if (
                slot != UNBOUND_SESSION
                and key == UNBOUND_SESSION
                and not allow_new_session_unbound
                and pkt.target_mode in RESERVED_UNBOUND_MODES
            ):
                # A capture reserved for a *new* session (or awaiting an explicit
                # "ask" choice) is never presented as belonging to an existing chat.
                continue
            return key, pkt
        return None

    def get_pending(self, session_id: str = "", *, packet_id: str = "") -> Packet | None:
        with self._lock, self._connect() as conn:
            found = self._resolve_row(conn, session_id, packet_id=packet_id)
            return found[1] if found else None

    def get_unbound_pending(self) -> Packet | None:
        with self._lock, self._connect() as conn:
            found = self._resolve_row(conn, "")
            return found[1] if found else None

    def record_hotkey_capture(
        self,
        pkt: Packet,
        hotkey_target: str,
        *,
        focus_composer: bool = True,
    ) -> dict[str, Any]:
        now = time.time()
        norm_target = normalize_hotkey_target(hotkey_target)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM runtime_state WHERE key = ?",
                ("hotkey_seq",),
            ).fetchone()
            try:
                seq = int(row[0]) + 1 if row else 1
            except (TypeError, ValueError):
                seq = 1
            conn.execute(
                "INSERT INTO runtime_state(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                ("hotkey_seq", str(seq), now),
            )
            evt: dict[str, Any] = {
                "seq": seq,
                "packet_id": pkt.id,
                "session_id": pkt.session_id,
                "target_mode": pkt.target_mode,
                "hotkey_target": norm_target,
                "mode": pkt.mode,
                "app_display": pkt.app_display,
                "title": pkt.raw_title,
                "focus_composer": bool(focus_composer),
                "captured_at": now,
            }
            conn.execute(
                "INSERT INTO runtime_state(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                ("last_hotkey_event", json.dumps(evt, ensure_ascii=False), now),
            )
        return evt

    def get_last_hotkey_event(self, max_age_seconds: float = 30.0) -> dict[str, Any] | None:
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value, updated_at FROM runtime_state WHERE key = ?",
                ("last_hotkey_event",),
            ).fetchone()
            if not row:
                return None
            raw_json, updated_at = str(row[0] or ""), float(row[1] or 0)
            if now - updated_at > max_age_seconds:
                return None
            try:
                evt = json.loads(raw_json)
            except Exception:
                return None
            if not isinstance(evt, dict) or not evt.get("packet_id"):
                return None
            # Only surface if that packet is still pending in the unbound slot
            # (not already retargeted, consumed, or discarded)
            still_there = self._resolve_row(conn, "", packet_id=str(evt["packet_id"]))
            if not still_there:
                return None
            slot_key, pkt = still_there
            if slot_key != UNBOUND_SESSION or pkt.session_id:
                return None
            hk_target = str(evt.get("hotkey_target") or "")
            if hk_target in {"new_session", "ask"} and pkt.target_mode != hk_target:
                return None
            return evt

    def retarget_pending(
        self,
        *,
        packet_id: str = "",
        from_session_id: str = "",
        to_session_id: str = "",
        target_mode: str | None = None,
        only_if_new_session: bool = False,
    ) -> Packet | None:
        new_sid = str(to_session_id or "").strip()
        new_slot = self._slot(new_sid)
        old_shot: str | None = None
        prev_sid = ""
        with self._lock, self._connect() as conn:
            found = self._resolve_row(conn, from_session_id, allow_new_session_unbound=True, packet_id=packet_id)
            if not found and not packet_id:
                found = self._resolve_row(conn, "", allow_new_session_unbound=True)
            if not found:
                return None
            old_slot, pkt = found
            if only_if_new_session and new_sid:
                started_at = lookup_session_started_at(new_sid)
                if started_at is not None and started_at < pkt.created_at - _SESSION_START_SKEW_SECONDS:
                    return None
            prev_sid = pkt.session_id
            pkt.session_id = new_sid
            pkt.target_mode = (
                normalize_target_mode(target_mode, has_session=bool(new_sid))
                if target_mode is not None
                else ("session" if new_sid else "new_session")
            )
            payload = json.dumps(pkt.to_storage_dict(), ensure_ascii=False)
            if old_slot != new_slot:
                displaced = conn.execute(
                    "SELECT payload_json FROM pending_packets WHERE slot_key = ?",
                    (new_slot,),
                ).fetchone()
                if displaced:
                    try:
                        prev_pkt = Packet.from_storage_dict(json.loads(displaced[0]))
                        if prev_pkt.screenshot_path and prev_pkt.screenshot_path != pkt.screenshot_path:
                            old_shot = prev_pkt.screenshot_path
                    except Exception:
                        pass
                conn.execute("DELETE FROM pending_packets WHERE slot_key = ?", (old_slot,))
            conn.execute(
                "INSERT INTO pending_packets(slot_key, packet_id, session_id, created_at, expires_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(slot_key) DO UPDATE SET "
                "packet_id=excluded.packet_id, session_id=excluded.session_id, "
                "created_at=excluded.created_at, expires_at=excluded.expires_at, payload_json=excluded.payload_json",
                (new_slot, pkt.id, pkt.session_id, pkt.created_at, pkt.expires_at, payload),
            )
            self._clear_last_hotkey_if_matches(conn, pkt.id)
        if old_shot:
            safe_unlink_shot(old_shot)
        if new_sid:
            self.set_focused_session(new_sid)
        from .render import format_public_packet

        emit_plugin_event(
            "state.changed",
            {
                "action": "retargeted",
                "previous_session_id": prev_sid,
                "session_id": pkt.session_id,
                "packet_id": pkt.id,
                "pending": format_public_packet(pkt, include_preview=True),
            },
        )
        return pkt

    def update_pending(
        self,
        session_id: str = "",
        *,
        packet_id: str = "",
        fields: list[str] | None = None,
        redact_enabled: bool | None = None,
        raw_tree_markdown: str | None = None,
        element_count: int | None = None,
        tree_truncated: bool | None = None,
    ) -> Packet | None:
        with self._lock, self._connect() as conn:
            found = self._resolve_row(
                conn, session_id, allow_new_session_unbound=True, packet_id=packet_id
            )
            if not found:
                return None
            slot, pkt = found
            if fields is not None:
                pkt.fields = normalize_fields(fields)
            if redact_enabled is not None:
                pkt.redact_enabled = bool(redact_enabled)
            if raw_tree_markdown is not None:
                pkt.raw_tree_markdown = str(raw_tree_markdown)
            if element_count is not None:
                pkt.element_count = int(element_count)
            if tree_truncated is not None:
                pkt.tree_truncated = bool(tree_truncated)
            payload = json.dumps(pkt.to_storage_dict(), ensure_ascii=False)
            conn.execute(
                "UPDATE pending_packets SET payload_json = ? WHERE slot_key = ?",
                (payload, slot),
            )
        from .render import format_public_packet

        emit_plugin_event(
            "state.changed",
            {
                "action": "updated",
                "session_id": pkt.session_id,
                "packet_id": pkt.id,
                "pending": format_public_packet(pkt, include_preview=True),
            },
        )
        return pkt

    def clear_pending(self, session_id: str = "", *, packet_id: str = "") -> bool:
        shot: str | None = None
        pkt_session = str(session_id or "").strip()
        with self._lock, self._connect() as conn:
            found = self._resolve_row(
                conn, session_id, allow_new_session_unbound=True, packet_id=packet_id
            )
            if not found:
                return False
            slot, pkt = found
            pkt_session = pkt.session_id
            shot = pkt.screenshot_path
            conn.execute("DELETE FROM pending_packets WHERE slot_key = ?", (slot,))
            self._clear_last_hotkey_if_matches(conn, pkt.id)
        safe_unlink_shot(shot)
        emit_plugin_event("state.changed", {"action": "cleared", "session_id": pkt_session, "pending": None})
        return True

    def stage_inline_shot(self, session_id: str, pkt: Packet) -> None:
        """Track an inline lat_grab screenshot so post_llm_call cleans it up after the turn."""
        clean_sid = str(session_id or "").strip()
        if not clean_sid or not pkt.screenshot_path:
            return
        deadline = time.time() + 180.0
        with self._lock, self._connect() as conn:
            self._write_active_turn_payload(conn, clean_sid, deadline, pkt.to_storage_dict())

    def consume_for_turn(
        self,
        session_id: str,
        *,
        is_first_turn: bool = True,
        platform: str = "",
        session_started_at: float | None = None,
    ) -> Packet | None:
        """Atomically pop the pending packet for *session_id* and stage it for vision middleware.

        An exact per-session packet always wins. An unbound packet is only claimed
        when :func:`unbound_claim_allowed` says so: ``ask`` captures wait for an
        explicit destination, and ``new_session`` captures are reserved for the
        first turn of a session created after the capture (never an older chat that
        merely happens to be focused).
        """
        clean_sid = str(session_id or "").strip()
        if not clean_sid:
            return None
        consumed_slot = ""
        started_at_cache: list[float | None] = []

        def _started_at() -> float | None:
            if session_started_at is not None:
                return session_started_at
            if not started_at_cache:
                started_at_cache.append(lookup_session_started_at(clean_sid))
            return started_at_cache[0]

        with self._lock, self._connect() as conn:
            # 1. Exact session match always wins
            found: tuple[str, Packet] | None = None
            row = conn.execute(
                "SELECT payload_json FROM pending_packets WHERE slot_key = ?",
                (clean_sid,),
            ).fetchone()
            now = time.time()
            if row:
                try:
                    exact_pkt = Packet.from_storage_dict(json.loads(row[0]))
                    if exact_pkt.is_expired(now):
                        conn.execute("DELETE FROM pending_packets WHERE slot_key = ?", (clean_sid,))
                        safe_unlink_shot(exact_pkt.screenshot_path)
                    else:
                        found = (clean_sid, exact_pkt)
                except Exception:
                    conn.execute("DELETE FROM pending_packets WHERE slot_key = ?", (clean_sid,))

            # 2. Otherwise check the unbound slot (guarded against cross-session theft)
            if found is None:
                ub = self._resolve_row(conn, "")
                if ub is not None:
                    _, ub_pkt = ub
                    needs_start = ub_pkt.target_mode == "new_session" and bool(is_first_turn)
                    if unbound_claim_allowed(
                        ub_pkt,
                        clean_sid,
                        is_first_turn=bool(is_first_turn),
                        platform=platform,
                        started_at=_started_at() if needs_start else None,
                    ):
                        found = ub

            if found is None:
                return None

            slot, pkt = found
            consumed_slot = slot
            conn.execute("DELETE FROM pending_packets WHERE slot_key = ?", (slot,))
            self._clear_last_hotkey_if_matches(conn, pkt.id)
            if "screenshot" in pkt.fields and pkt.screenshot_path:
                deadline = time.time() + 180.0
                self._write_active_turn_payload(conn, clean_sid, deadline, pkt.to_storage_dict())
            else:
                # Screenshot excluded by toggle -> delete immediately!
                safe_unlink_shot(pkt.screenshot_path)
                pkt.screenshot_path = None
        emit_plugin_event(
            "state.changed",
            {
                "action": "consumed",
                "session_id": "" if consumed_slot == UNBOUND_SESSION else clean_sid,
                "consumed_by": clean_sid,
                "packet_id": pkt.id,
                "pending": None,
            },
        )
        return pkt

    def active_turn_packet(self, session_id: str) -> Packet | None:
        if not session_id:
            return None
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT expires_at, payload_json FROM active_turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            expires_at, payload_json = float(row[0]), str(row[1])
            try:
                pkt = Packet.from_storage_dict(json.loads(payload_json))
            except Exception:
                conn.execute("DELETE FROM active_turns WHERE session_id = ?", (session_id,))
                return None
            if expires_at <= now:
                conn.execute("DELETE FROM active_turns WHERE session_id = ?", (session_id,))
                for shot in set(self._collect_turn_shots_from_json(payload_json)):
                    safe_unlink_shot(shot)
                return None
            return pkt

    def finish_turn(self, session_id: str) -> None:
        """Delete the temporary screenshot file as soon as the turn finishes."""
        if not session_id:
            return
        shots: list[str] = []
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM active_turns WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row:
                shots.extend(self._collect_turn_shots_from_json(str(row[0] or "")))
            conn.execute("DELETE FROM active_turns WHERE session_id = ?", (session_id,))
        for s in set(shots):
            safe_unlink_shot(s)

    def sweep_expired(self, max_age_seconds: int = 360) -> int:
        """Remove expired DB rows and orphaned PNG files."""
        now = time.time()
        removed = 0
        expired_shots: list[str] = []
        active_shots: set[Path] = set()
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute("SELECT slot_key, expires_at, payload_json FROM pending_packets").fetchall()
                for slot, expires_at, payload_json in rows:
                    try:
                        pkt = Packet.from_storage_dict(json.loads(payload_json))
                    except Exception:
                        conn.execute("DELETE FROM pending_packets WHERE slot_key = ?", (slot,))
                        removed += 1
                        continue
                    if float(expires_at) <= now:
                        conn.execute("DELETE FROM pending_packets WHERE slot_key = ?", (slot,))
                        if pkt.screenshot_path:
                            expired_shots.append(pkt.screenshot_path)
                        removed += 1
                    elif pkt.screenshot_path:
                        try:
                            active_shots.add(Path(pkt.screenshot_path).resolve())
                        except OSError:
                            pass

                trows = conn.execute("SELECT session_id, expires_at, payload_json FROM active_turns").fetchall()
                for tsid, texpires, tpayload in trows:
                    turn_shots = self._collect_turn_shots_from_json(str(tpayload or ""))
                    try:
                        Packet.from_storage_dict(json.loads(tpayload))
                    except Exception:
                        conn.execute("DELETE FROM active_turns WHERE session_id = ?", (tsid,))
                        expired_shots.extend(turn_shots)
                        removed += 1
                        continue
                    if float(texpires) <= now:
                        conn.execute("DELETE FROM active_turns WHERE session_id = ?", (tsid,))
                        expired_shots.extend(turn_shots)
                        removed += 1
                    else:
                        for tshot in turn_shots:
                            try:
                                active_shots.add(Path(tshot).resolve())
                            except OSError:
                                pass

        for shot in expired_shots:
            safe_unlink_shot(shot)

        cutoff = now - max(90, max_age_seconds)
        try:
            for png in get_shots_dir().glob("lat_*.png"):
                try:
                    resolved = png.resolve()
                    if resolved not in active_shots and png.stat().st_mtime < cutoff:
                        png.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    continue
        except OSError:
            pass
        return removed


STORE = EphemeralStore()
