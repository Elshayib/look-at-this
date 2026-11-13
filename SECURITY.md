# Security Policy

## Privacy & Security Architecture

**Look At This** captures local desktop windows and screen content on demand, so privacy and security boundaries are foundational invariants:

- **User-Initiated Only**: No background screen recording, continuous polling, or telemetry.
- **Zero Network Footprint**: Makes no outbound HTTP requests and loads no remote scripts.
- **Pre-Capture Exclusions**: Password managers (`excluded_apps`) and private/incognito browser windows (`excluded_title_patterns`) are refused prior to pixel capture, including Z-order occlusion checks for region snips and overlays.
- **Local Ephemeral Storage**: Queued state lives strictly in `<HERMES_HOME>/plugin-data/look-at-this/` (`0700` on POSIX) and temporary PNG files are unlinked immediately upon turn consumption, cancellation, replacement, or TTL expiration.
- **Path Safety**: `safe_unlink_shot()` refuses to delete files outside `<HERMES_HOME>/plugin-data/look-at-this/shots/`.
- **Prompt Injection Defense**: Captured text is wrapped in explicit `UNTRUSTED SCREEN DATA` framing, and embedded `[look-at-this packet]` / `[/look-at-this packet]` strings in captured content are neutralized.
- **Reversible Secret Redaction**: When enabled, common tokens, keys, credentials, emails, Luhn-valid card numbers, IBANs, and sensitive URL query parameters are masked in text context prior to model injection.

---

## Supported Versions

| Version | Supported |
|---|---|
| `0.2.x` | Yes (current release series) |
| `< 0.2.0` | Upgrade via `hermes plugins update look-at-this` |

---

## Reporting a Vulnerability

If you discover a security or privacy vulnerability—such as:

- A bypass of `excluded_apps` or `excluded_title_patterns` (including occlusion/BitBlt/region capture bypasses)
- Cross-session leakage of captured screens into another session, subagent, cron job, or background turn
- Arbitrary file deletion / path traversal in `shots/` cleanup
- Prompt-injection boundary breakout in rendered packet context
- Sensitive file exposure via Desktop API routes

**Please do not open a public GitHub issue.**

Instead, report it privately via **GitHub Private Vulnerability Reporting**:

1. Go to **Security → Advisories → Report a vulnerability** on [`Elshayib/look-at-this`](https://github.com/Elshayib/look-at-this/security/advisories/new).
2. Include the affected version (`/look version`), OS/display server, reproduction steps, and potential impact.

We aim to acknowledge reports promptly, validate the fix with regression tests, and publish an advisory and patch release once resolved.
