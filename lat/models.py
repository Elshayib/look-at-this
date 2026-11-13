"""Domain models, errors, and live configuration for Look At This v2."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields as dataclass_fields
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Callable
import uuid

SCHEMA_VERSION = "look-at-this/packet@2"
DEFAULT_TTL_SECONDS = 300
MAX_TIMESTAMP = 253_402_300_799.0  # 9999-12-31T23:59:59Z

ALL_FIELDS = ("screenshot", "context", "tree", "url", "selection")
DEFAULT_FIELDS = ("screenshot", "context", "url", "selection")
UNBOUND_SESSION = "__unbound__"
VALID_HOTKEY_TARGETS = ("new_session", "ask", "current_session")
VALID_TARGET_MODES = ("new_session", "ask", "session", "any")


def normalize_hotkey_target(raw: Any) -> str:
    clean = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if clean in {"ask", "prompt", "choose", "picker"}:
        return "ask"
    if clean in {"current", "current_session", "active", "active_session", "same"}:
        return "current_session"
    return "new_session"


def normalize_target_mode(raw: Any, *, has_session: bool = False) -> str:
    clean = str(raw or "").strip().lower().replace("-", "_")
    if clean in VALID_TARGET_MODES:
        return clean
    return "session" if has_session else "any"

DEFAULT_EXCLUDED_APPS = [
    "1password",
    "1password7",
    "bitwarden",
    "keepass",
    "keepassxc",
    "lastpass",
    "dashlane",
    "nordpass",
    "enpass",
    "keeper",
    "protonpass",
]

# Only genuine private/incognito browser window markers by default — never broad
# words like "password" or "unlock" that falsely block code editors or docs.
DEFAULT_EXCLUDED_TITLES = [
    r"(?i)\b(?:incognito|inprivate)\b",
    r"(?i)\bprivate\s+(?:browsing|window)\b",
]


class LookAtError(RuntimeError):
    """Structured user-facing error with a stable machine-readable code."""

    code = "look_at_error"

    def __init__(self, message: str, *, detail: str = "", code: str | None = None):
        super().__init__(message)
        self.detail = detail
        if code:
            self.code = code

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "code": self.code, "error": str(self)}
        if self.detail:
            out["detail"] = self.detail
        return out


class ExcludedWindowError(LookAtError):
    code = "excluded_window"


class CaptureCancelledError(LookAtError):
    code = "cancelled"


class NoWindowFoundError(LookAtError):
    code = "no_window"


class CaptureBackendError(LookAtError):
    code = "capture_failed"


def friendly_app_name(raw_app: str) -> str:
    """Turn 'msedge.exe' or 'Code.exe' into a clean human display label."""
    stem = re.sub(r"\.(?:exe|app|bin)$", "", str(raw_app or "").strip(), flags=re.IGNORECASE)
    known = {
        "code": "VS Code",
        "code - insiders": "VS Code Insiders",
        "cursor": "Cursor",
        "windsurf": "Windsurf",
        "chrome": "Chrome",
        "msedge": "Edge",
        "firefox": "Firefox",
        "brave": "Brave",
        "vivaldi": "Vivaldi",
        "arc": "Arc",
        "zen": "Zen",
        "windowsterminal": "Windows Terminal",
        "wt": "Windows Terminal",
        "powershell": "PowerShell",
        "pwsh": "PowerShell",
        "cmd": "Command Prompt",
        "wezterm-gui": "WezTerm",
        "alacritty": "Alacritty",
        "kitty": "Kitty",
        "explorer": "File Explorer",
        "finder": "Finder",
        "slack": "Slack",
        "discord": "Discord",
        "telegram": "Telegram",
        "notion": "Notion",
        "obsidian": "Obsidian",
        "figma": "Figma",
    }
    low = stem.lower()
    if low in known:
        return known[low]
    return stem[:1].upper() + stem[1:] if stem else "Unknown"


def safe_iso(ts: float) -> str:
    try:
        val = float(ts)
        if not math.isfinite(val):
            return "unknown"
        val = min(max(val, 0.0), MAX_TIMESTAMP)
        return datetime.fromtimestamp(val, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return "unknown"


def normalize_fields(fields: Any, *, include_tree_default: bool = False) -> list[str]:
    if isinstance(fields, (list, tuple)):
        picked = [str(f).strip().lower() for f in fields if str(f).strip().lower() in ALL_FIELDS]
        # Preserve canonical ordering without duplicates
        seen: set[str] = set()
        ordered = [f for f in ALL_FIELDS if f in picked and not (f in seen or seen.add(f))]
        if ordered:
            return ordered
    base = list(DEFAULT_FIELDS)
    if include_tree_default and "tree" not in base:
        base.insert(2, "tree")
    return base


@dataclass(frozen=True)
class RegionRect:
    x: int
    y: int
    width: int
    height: int

    def to_dict(self) -> dict[str, int]:
        return {"x": int(self.x), "y": int(self.y), "width": int(self.width), "height": int(self.height)}

    @classmethod
    def from_dict(cls, data: Any) -> "RegionRect | None":
        if not isinstance(data, dict):
            return None
        try:
            w = int(data.get("width") or 0)
            h = int(data.get("height") or 0)
            if w <= 0 or h <= 0:
                return None
            return cls(x=int(data.get("x") or 0), y=int(data.get("y") or 0), width=w, height=h)
        except (TypeError, ValueError):
            return None


@dataclass
class WindowInfo:
    window_id: int | str
    pid: int
    app: str
    title: str
    bounds: RegionRect
    z_order: int = 0
    is_active: bool = False
    is_excluded: bool = False
    exclusion_reason: str = ""
    thumbnail_data_url: str | None = None

    @property
    def app_display(self) -> str:
        return friendly_app_name(self.app)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "pid": self.pid,
            "app": self.app,
            "app_display": self.app_display,
            "title": self.title,
            "bounds": self.bounds.to_dict(),
            "z_order": self.z_order,
            "is_active": self.is_active,
            "is_excluded": self.is_excluded,
            "exclusion_reason": self.exclusion_reason,
            "thumbnail_data_url": self.thumbnail_data_url,
        }


@dataclass
class SmartContext:
    focused_role: str = ""
    focused_name: str = ""
    focused_value: str = ""
    highlights: list[str] = field(default_factory=list)
    tree_markdown: str = ""
    element_count: int = 0
    truncated: bool = False

    def render_compact(self) -> str:
        lines: list[str] = []
        if self.focused_role or self.focused_name or self.focused_value:
            focus_parts = [p for p in (self.focused_role, self.focused_name) if p]
            head = " — ".join(focus_parts) if focus_parts else "Element"
            if self.focused_value:
                val = self.focused_value.strip()
                if len(val) > 280:
                    val = val[:279] + "…"
                lines.append(f"Focused control: {head} (value: {val})")
            else:
                lines.append(f"Focused control: {head}")
        if self.highlights:
            lines.append("Visible UI / status / error text:")
            for item in self.highlights[:24]:
                lines.append(f"  • {item}")
        return "\n".join(lines)


@dataclass
class RawCapture:
    capture_id: str
    mode: str  # "window" | "region"
    app: str
    pid: int
    window_id: int | str | None
    title: str
    bounds: RegionRect | None
    url: str | None = None
    selection: str | None = None
    context_text: str = ""
    tree_markdown: str = ""
    element_count: int = 0
    tree_truncated: bool = False
    screenshot_path: str | None = None
    screenshot_width: int | None = None
    screenshot_height: int | None = None
    thumbnail_data_url: str | None = None


@dataclass
class Settings:
    hotkey_target: str = "new_session"
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    window_hotkey: str = "ctrl+alt+l"
    snip_hotkey: str = "ctrl+alt+s"
    focus_hermes_on_hotkey: bool = True
    hotkey_hud_feedback: bool = True
    vision_injection: bool = True
    default_redact: bool = False
    include_full_tree_by_default: bool = False
    excluded_apps: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDED_APPS))
    excluded_title_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDED_TITLES))
    max_tree_elements: int = 200
    max_image_dim: int = 1920

    @classmethod
    def from_getter(cls, getter: Callable[..., Any] | None) -> "Settings":
        defaults = cls()
        if getter is None:
            return defaults

        def _get(key: str, default: Any) -> Any:
            try:
                val = getter(key, default=default)
            except TypeError:
                val = getter(key)
            return default if val is None else val

        def _int(key: str, default: int, lo: int, hi: int) -> int:
            try:
                return max(lo, min(hi, int(_get(key, default))))
            except (TypeError, ValueError):
                return default

        def _bool(key: str, default: bool) -> bool:
            val = _get(key, default)
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.strip().lower() in {"1", "true", "yes", "on"}
            return bool(val)

        apps = _get("excluded_apps", defaults.excluded_apps)
        pats = _get("excluded_title_patterns", defaults.excluded_title_patterns)
        win_hk = _get("window_hotkey", defaults.window_hotkey)
        snip_hk = _get("snip_hotkey", defaults.snip_hotkey)
        hk_target = normalize_hotkey_target(_get("hotkey_target", defaults.hotkey_target))

        return cls(
            hotkey_target=hk_target,
            ttl_seconds=_int("ttl_seconds", DEFAULT_TTL_SECONDS, 30, 3600),
            window_hotkey=str(win_hk).strip() if win_hk is not None else defaults.window_hotkey,
            snip_hotkey=str(snip_hk).strip() if snip_hk is not None else defaults.snip_hotkey,
            focus_hermes_on_hotkey=_bool("focus_hermes_on_hotkey", True),
            hotkey_hud_feedback=_bool("hotkey_hud_feedback", True),
            vision_injection=_bool("vision_injection", True),
            default_redact=_bool("default_redact", False),
            include_full_tree_by_default=_bool("include_full_tree_by_default", False),
            excluded_apps=[str(x).strip() for x in apps if str(x).strip()]
            if isinstance(apps, (list, tuple))
            else defaults.excluded_apps,
            excluded_title_patterns=[str(x).strip() for x in pats if str(x).strip()]
            if isinstance(pats, (list, tuple))
            else defaults.excluded_title_patterns,
            max_tree_elements=_int("max_tree_elements", 200, 20, 1500),
            max_image_dim=_int("max_image_dim", 1920, 640, 3840),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _hermes_config_path() -> Path:
    home = os.environ.get("HERMES_HOME")
    base = Path(home) if home else (Path.home() / ".hermes")
    return base / "config.yaml"


def _read_yaml_plugin_settings() -> tuple[dict[str, Any], float]:
    cfg_path = _hermes_config_path()
    try:
        mtime = cfg_path.stat().st_mtime if cfg_path.exists() else 0.0
    except OSError:
        mtime = 0.0

    cfg: dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config_readonly

        loaded = load_config_readonly()
        if isinstance(loaded, dict):
            cfg = loaded
    except Exception:
        if cfg_path.exists():
            try:
                import yaml  # type: ignore[import-untyped]

                loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cfg = loaded
            except Exception:
                pass

    entry = (((cfg.get("plugins") or {}).get("entries") or {}).get("look-at-this") or {})
    settings_map = entry.get("settings")
    if isinstance(settings_map, dict):
        return dict(settings_map), mtime
    return {}, mtime


def write_yaml_plugin_setting(key: str, value: Any) -> bool:
    """Persist a plugin setting into Hermes config.yaml when available."""
    cfg_path = _hermes_config_path()
    try:
        from hermes_cli.config import load_config, save_config  # type: ignore[attr-defined]

        cfg = load_config()
        if not isinstance(cfg, dict):
            cfg = {}
        plugins = cfg.setdefault("plugins", {})
        entries = plugins.setdefault("entries", {})
        entry = entries.setdefault("look-at-this", {})
        settings_map = entry.setdefault("settings", {})
        settings_map[key] = value
        save_config(cfg)
        return True
    except Exception:
        pass

    if not cfg_path.exists():
        return False
    try:
        import yaml  # type: ignore[import-untyped]

        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        cfg = loaded if isinstance(loaded, dict) else {}
        plugins = cfg.setdefault("plugins", {})
        entries = plugins.setdefault("entries", {})
        entry = entries.setdefault("look-at-this", {})
        settings_map = entry.setdefault("settings", {})
        settings_map[key] = value
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        return True
    except Exception:
        return False


def load_live_settings(override_getter: Callable[..., Any] | None = None) -> Settings:
    """Load live settings from config.yaml (or an explicit ctx.get_config callable)."""
    if override_getter is not None:
        raw: dict[str, Any] = {}
        for f in dataclass_fields(Settings):
            try:
                val = override_getter(f.name, default=None)
            except TypeError:
                val = override_getter(f.name)
            if val is not None:
                raw[f.name] = val
        cfg_mtime = 0.0
    else:
        raw, cfg_mtime = _read_yaml_plugin_settings()

    merged = dict(raw)
    try:
        from .store import STORE

        for f in dataclass_fields(Settings):
            entry = STORE.get_preference_entry(f.name)
            if not entry:
                continue
            has_yaml_val = f.name in raw and raw[f.name] is not None
            if entry.get("legacy"):
                # Legacy bare SQLite override: respect config.yaml whenever it specifies a value
                # or was modified after the SQLite row.
                if has_yaml_val or (cfg_mtime > float(entry.get("updated_at") or 0.0)):
                    STORE.clear_preference_override(f.name)
                    continue
            else:
                baseline = entry.get("baseline")
                saved_mtime = float(entry.get("config_mtime") or 0.0)
                yaml_val = raw.get(f.name)
                if (has_yaml_val and yaml_val != baseline) or (
                    cfg_mtime > 0.0 and saved_mtime > 0.0 and cfg_mtime > saved_mtime + 1e-6 and has_yaml_val
                ) or (cfg_mtime > 0.0 and saved_mtime == 0.0 and has_yaml_val):
                    STORE.clear_preference_override(f.name)
                    continue
            merged[f.name] = entry.get("value")
    except Exception:
        pass

    return Settings.from_getter(lambda k, default=None: merged.get(k, default))


@dataclass
class Packet:
    id: str
    session_id: str
    created_at: float
    expires_at: float
    mode: str  # "window" | "region"
    app: str
    pid: int
    window_id: int | str | None
    bounds: RegionRect | None
    raw_title: str
    raw_url: str | None
    raw_selection: str | None
    raw_context_text: str
    raw_tree_markdown: str
    element_count: int
    tree_truncated: bool
    screenshot_path: str | None
    screenshot_width: int | None
    screenshot_height: int | None
    thumbnail_data_url: str | None
    fields: list[str]
    redact_enabled: bool = False
    target_mode: str = "any"

    @property
    def app_display(self) -> str:
        return friendly_app_name(self.app)

    def is_expired(self, now: float | None = None) -> bool:
        return self.expires_at <= (time.time() if now is None else now)

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "id": self.id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "mode": self.mode,
            "app": self.app,
            "pid": self.pid,
            "window_id": self.window_id,
            "bounds": self.bounds.to_dict() if self.bounds else None,
            "raw_title": self.raw_title,
            "raw_url": self.raw_url,
            "raw_selection": self.raw_selection,
            "raw_context_text": self.raw_context_text,
            "raw_tree_markdown": self.raw_tree_markdown,
            "element_count": self.element_count,
            "tree_truncated": self.tree_truncated,
            "screenshot_path": self.screenshot_path,
            "screenshot_width": self.screenshot_width,
            "screenshot_height": self.screenshot_height,
            "thumbnail_data_url": self.thumbnail_data_url,
            "fields": list(self.fields),
            "redact_enabled": bool(self.redact_enabled),
            "target_mode": normalize_target_mode(self.target_mode, has_session=bool(self.session_id)),
        }

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> "Packet":
        if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
            raise ValueError("Invalid Look At This packet schema")
        now = time.time()

        def _ts(key: str, fallback: float) -> float:
            try:
                v = float(data.get(key, fallback))
                return v if math.isfinite(v) and 0 < v <= MAX_TIMESTAMP else fallback
            except (TypeError, ValueError):
                return fallback

        def _opt_int(key: str) -> int | None:
            val = data.get(key)
            if val is None:
                return None
            try:
                return int(val)
            except (TypeError, ValueError):
                return None

        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            session_id=str(data.get("session_id") or ""),
            created_at=_ts("created_at", now),
            expires_at=_ts("expires_at", now + DEFAULT_TTL_SECONDS),
            mode="region" if data.get("mode") == "region" else "window",
            app=str(data.get("app") or "unknown"),
            pid=_opt_int("pid") or 0,
            window_id=data.get("window_id"),
            bounds=RegionRect.from_dict(data.get("bounds")),
            raw_title=str(data.get("raw_title") or ""),
            raw_url=str(data["raw_url"]) if data.get("raw_url") else None,
            raw_selection=str(data["raw_selection"]) if data.get("raw_selection") else None,
            raw_context_text=str(data.get("raw_context_text") or ""),
            raw_tree_markdown=str(data.get("raw_tree_markdown") or ""),
            element_count=_opt_int("element_count") or 0,
            tree_truncated=bool(data.get("tree_truncated")),
            screenshot_path=str(data["screenshot_path"]) if data.get("screenshot_path") else None,
            screenshot_width=_opt_int("screenshot_width"),
            screenshot_height=_opt_int("screenshot_height"),
            thumbnail_data_url=str(data["thumbnail_data_url"]) if data.get("thumbnail_data_url") else None,
            fields=normalize_fields(data.get("fields")),
            redact_enabled=bool(data.get("redact_enabled", False)),
            target_mode=normalize_target_mode(
                data.get("target_mode"), has_session=bool(str(data.get("session_id") or "").strip())
            ),
        )
