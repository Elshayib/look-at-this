"""Window privacy exclusions and toggleable sensitive-text redaction."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Packet, Settings


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
