"""Window privacy exclusions and toggleable sensitive-text redaction."""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Packet, RegionRect, Settings, WindowInfo


def _normalize_app_token(app: str) -> str:
    clean = str(app or "").strip().lower()
    clean = re.sub(r"\.(?:exe|app|bin)$", "", clean)
    return clean


def check_window_exclusion(app: str, title: str, settings: Settings) -> str | None:
    """Return human-readable rule if the window must be refused, else None.

    Matches only configured password-manager apps and private/incognito browser
    patterns by default — never generic words in normal code/doc titles.
    """
    norm_app = _normalize_app_token(app)
    raw_app = str(app or "").strip().lower()
    excluded_apps = {_normalize_app_token(x) for x in settings.excluded_apps if x}
    excluded_raw = {str(x).strip().lower() for x in settings.excluded_apps if x}
    if (norm_app and norm_app in excluded_apps) or (raw_app and raw_app in excluded_raw):
        return f"excluded app ({app})"

    title_str = str(title or "")
    for pat in settings.excluded_title_patterns:
        if not pat:
            continue
        try:
            if re.search(pat, title_str):
                return f"private/excluded window pattern ({pat})"
        except re.error:
            # Fail closed on malformed user regex so a typo never leaks a blocked window
            return f"invalid exclusion pattern ({pat})"
    return None


def _coerce_rect_tuple(rect: RegionRect | dict[str, Any] | None) -> tuple[int, int, int, int] | None:
    if isinstance(rect, RegionRect):
        if rect.width <= 0 or rect.height <= 0:
            return None
        return (int(rect.x), int(rect.y), int(rect.x) + int(rect.width), int(rect.y) + int(rect.height))
    if isinstance(rect, dict):
        try:
            x = int(rect.get("x") or 0)
            y = int(rect.get("y") or 0)
            w = int(rect.get("width") or 0)
            h = int(rect.get("height") or 0)
        except (TypeError, ValueError):
            return None
        if w <= 0 or h <= 0:
            return None
        return (x, y, x + w, y + h)
    return None


def _win_field(win: WindowInfo | dict[str, Any], name: str, default: Any = None) -> Any:
    if isinstance(win, dict):
        return win.get(name, default)
    return getattr(win, name, default)


def _intersect_rect(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int]
) -> tuple[int, int, int, int] | None:
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _subtract_rect(
    rect: tuple[int, int, int, int], cutter: tuple[int, int, int, int]
) -> list[tuple[int, int, int, int]]:
    inter = _intersect_rect(rect, cutter)
    if inter is None:
        return [rect]
    rx0, ry0, rx1, ry1 = rect
    ix0, iy0, ix1, iy1 = inter
    pieces: list[tuple[int, int, int, int]] = []
    if iy0 > ry0:
        pieces.append((rx0, ry0, rx1, iy0))
    if iy1 < ry1:
        pieces.append((rx0, iy1, rx1, ry1))
    if ix0 > rx0:
        pieces.append((rx0, iy0, ix0, iy1))
    if ix1 < rx1:
        pieces.append((ix1, iy0, rx1, iy1))
    return pieces


def unoccluded_excluded_rects(
    rect: RegionRect | dict[str, Any],
    windows: Iterable[WindowInfo | dict[str, Any]],
    *,
    stop_at_window_id: int | str | None = None,
) -> list[RegionRect]:
    """Return visible, unoccluded sub-rectangles of excluded windows overlapping *rect*."""
    target = _coerce_rect_tuple(rect)
    if target is None:
        return []
    occluders: list[tuple[int, int, int, int]] = []
    visible: list[RegionRect] = []
    stop_str = str(stop_at_window_id) if stop_at_window_id is not None else None

    for w in windows:
        wid = _win_field(w, "window_id")
        if stop_str is not None and wid is not None and str(wid) == stop_str:
            break
        wb = _coerce_rect_tuple(_win_field(w, "bounds"))
        if wb is None:
            continue
        if bool(_win_field(w, "is_excluded")):
            inter = _intersect_rect(target, wb)
            if inter is not None:
                rem = [inter]
                for occ in occluders:
                    next_rem: list[tuple[int, int, int, int]] = []
                    for piece in rem:
                        next_rem.extend(_subtract_rect(piece, occ))
                    rem = next_rem
                    if not rem:
                        break
                for rx0, ry0, rx1, ry1 in rem:
                    visible.append(RegionRect(x=rx0, y=ry0, width=rx1 - rx0, height=ry1 - ry0))
        occluders.append(wb)
    return visible


def find_overlapping_excluded_window(
    rect: RegionRect | dict[str, Any],
    windows: Iterable[WindowInfo | dict[str, Any]],
    *,
    stop_at_window_id: int | str | None = None,
) -> WindowInfo | dict[str, Any] | None:
    """Return the frontmost excluded window with visible unoccluded overlap on *rect*."""
    target = _coerce_rect_tuple(rect)
    if target is None:
        return None
    occluders: list[tuple[int, int, int, int]] = []
    stop_str = str(stop_at_window_id) if stop_at_window_id is not None else None

    for w in windows:
        wid = _win_field(w, "window_id")
        if stop_str is not None and wid is not None and str(wid) == stop_str:
            break
        wb = _coerce_rect_tuple(_win_field(w, "bounds"))
        if wb is None:
            continue
        if bool(_win_field(w, "is_excluded")):
            inter = _intersect_rect(target, wb)
            if inter is not None:
                rem = [inter]
                for occ in occluders:
                    next_rem: list[tuple[int, int, int, int]] = []
                    for piece in rem:
                        next_rem.extend(_subtract_rect(piece, occ))
                    rem = next_rem
                    if not rem:
                        break
                if rem:
                    return w
        occluders.append(wb)
    return None


def _luhn_valid(digits: str) -> bool:
    total = 0
    alt = False
    for ch in reversed(digits):
        if not ch.isdigit():
            return False
        n = ord(ch) - 48
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    return total % 10 == 0


def _iban_valid(value: str) -> bool:
    compact = re.sub(r"\s+", "", value).upper()
    if not (15 <= len(compact) <= 34) or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", compact):
        return False
    rearranged = compact[4:] + compact[:4]
    try:
        numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    except ValueError:
        return False
    return int(numeric) % 97 == 1


_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("api_key", re.compile(r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{32,}|glpat-[A-Za-z0-9_\-]{16,})\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\b")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
)

_CARD_RE = re.compile(r"(?<![\d.])(?:\d[ -]?){12,18}\d(?![\d.])")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9 ]{11,32}\b")
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)^(?:token|access_token|refresh_token|id_token|key|api_key|apikey|secret|client_secret|"
    r"password|passwd|pwd|session|sessionid|sid|auth|authorization|code|sig|signature|otp|nonce)$"
)


def redact_text(text: str | None, counts: dict[str, int] | None = None) -> str | None:
    if not text:
        return text
    tally = counts if counts is not None else {}
    out = text

    for label, pattern in _TEXT_PATTERNS:
        def _repl(_m: re.Match[str], _label: str = label) -> str:
            tally[_label] = tally.get(_label, 0) + 1
            return f"[REDACTED:{_label}]"

        out = pattern.sub(_repl, out)

    def _card_repl(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            tally["card"] = tally.get("card", 0) + 1
            return "[REDACTED:card]"
        return m.group(0)

    out = _CARD_RE.sub(_card_repl, out)

    def _iban_repl(m: re.Match[str]) -> str:
        if _iban_valid(m.group(0)):
            tally["iban"] = tally.get("iban", 0) + 1
            return "[REDACTED:iban]"
        return m.group(0)

    out = _IBAN_RE.sub(_iban_repl, out)
    return out


def redact_url(url: str | None, counts: dict[str, int] | None = None) -> str | None:
    if not url:
        return url
    tally = counts if counts is not None else {}
    try:
        parts = urlsplit(url)
    except ValueError:
        return redact_text(url, tally)

    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        parts = parts._replace(netloc=netloc)
        tally["url_auth"] = tally.get("url_auth", 0) + 1

    if parts.query:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        changed = 0
        cleaned = []
        for k, v in pairs:
            if _SENSITIVE_QUERY_RE.match(k):
                cleaned.append((k, "[REDACTED]"))
                changed += 1
            else:
                cleaned.append((k, v))
        if changed:
            tally["url_param"] = tally.get("url_param", 0) + changed
            parts = parts._replace(query=urlencode(cleaned, doseq=True))

    return urlunsplit(parts)


def resolve_packet_texts(pkt: Packet) -> tuple[dict[str, str | None], dict[str, int], dict[str, int]]:
    """Compute effective texts + applied redactions + potential redaction counts.

    Returns ``(effective_fields_dict, applied_counts, detected_counts)`` so the UI
    can show how many sensitive items were detected even when redaction is toggled off!
    """
    detected: dict[str, int] = {}
    red_title = redact_text(pkt.raw_title, detected) or ""
    red_url = redact_url(pkt.raw_url if "url" in pkt.fields else None, detected)
    red_sel = redact_text(pkt.raw_selection if "selection" in pkt.fields else None, detected)
    red_ctx = redact_text(pkt.raw_context_text if "context" in pkt.fields else "", detected) or ""
    red_tree = redact_text(pkt.raw_tree_markdown if "tree" in pkt.fields else "", detected) or ""

    if pkt.redact_enabled:
        return (
            {
                "title": red_title,
                "url": red_url,
                "selection": red_sel,
                "context": red_ctx,
                "tree": red_tree,
            },
            dict(detected),
            detected,
        )

    return (
        {
            "title": pkt.raw_title,
            "url": pkt.raw_url if "url" in pkt.fields else None,
            "selection": pkt.raw_selection if "selection" in pkt.fields else None,
            "context": pkt.raw_context_text if "context" in pkt.fields else "",
            "tree": pkt.raw_tree_markdown if "tree" in pkt.fields else "",
        },
        {},
        detected,
    )
