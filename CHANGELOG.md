# Changelog

All notable changes to **Look At This** are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- `/look version` (and `/look update` alias) displaying the installed plugin version and Hermes update commands.
- Installed plugin `version` field in `GET /api/plugins/look-at-this/status` and version badge + update guidance in the Hermes Desktop Settings page.
- `homepage` and `tags` metadata in `plugin.yaml`.
- Updating documentation in `README.md` and `skills/look-at-this/SKILL.md` (`hermes plugins update look-at-this`, `/plugins update`, `hermes plugins check-updates`, `hermes plugins auto-update on`, `hermes plugins adopt look-at-this`, and Hermes Desktop UI).
- Community & contributor files: GitHub issue forms (`.github/ISSUE_TEMPLATE/`), PR template (`.github/PULL_REQUEST_TEMPLATE.md`), `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and `CHANGELOG.md`.

---

## [0.2.1]

### Changed
- Polished user-facing copy, Desktop UI tooltips, and internal module docstrings to describe capabilities cleanly without internal version labels.

---

## [0.2.0]

### Added
- Native multi-monitor interactive snip & window-hover GDI overlay on Windows (`Ctrl+Alt+S`) with Per-Monitor-V2 DPI scaling.
- `hotkey_target` routing modes (`new_session`, `ask`, `current_session`) with "Where should this screen capture go?" modal in Hermes Desktop and new-session protection against stale/background session hijacking.
- Warm persistent PowerShell `UIAutomation` worker (`lat/capture/uia_worker.py`) for fast active URL, selection, focused element, and full accessibility tree extraction.
- Real-time Hermes Desktop UI extensions: `Screen Attached` composer card, Window Picker grid with live thumbnails, Lightbox + Packet Inspector modal, status bar menu, command palette actions, and Settings page.
- Pre-capture Z-order occlusion checks for excluded apps/titles, expanded reversible secret redaction, and delimiter-spoofing protection.
- Cross-platform CI workflow across Windows, macOS, and Linux.

---

## [0.1.0]

### Added
- Initial Look At This plugin for Hermes Agent: `/look` slash command, `lat_grab` / `lat_windows` / `lat_status` / `lat_clear` agent tools, SQLite WAL ephemeral packet store, one-turn `llm_request` vision middleware, and automatic post-turn PNG deletion.
