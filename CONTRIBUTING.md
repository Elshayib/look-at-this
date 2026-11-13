# Contributing to Look At This

Thank you for your interest in contributing to **Look At This**! Whether you are fixing a capture quirk on Wayland, refining Win32 DPI handling, improving Hermes Desktop UI polish, or updating documentation, we welcome your help.

---

## Architecture Overview

Look At This follows Hermes Agent's **unified two-half plugin architecture**:

1. **Agent Plugin Backend** (`plugin.yaml`, `__init__.py`, `lat/`, `dashboard/plugin_api.py`, `skills/look-at-this/SKILL.md`):
   - Registers tools (`lat_grab`, `lat_windows`, `lat_status`, `lat_clear`), hooks (`pre_llm_call`, `post_llm_call`, `on_session_end`), middleware (`llm_request`), and `/look`.
   - Mounts authenticated FastAPI routes under `/api/plugins/look-at-this/`.
   - Stores transient packets in `<HERMES_HOME>/plugin-data/look-at-this/data.db` (SQLite WAL) and temporary screenshots in `<HERMES_HOME>/plugin-data/look-at-this/shots/`.
2. **Hermes Desktop Frontend** (`dashboard/manifest.json`, `desktop/plugin.js`):
   - Built with zero external npm dependencies using Hermes's `@hermes/plugin-sdk`.
   - Renders the `Screen Attached` composer card, Window Picker dialog, Packet Inspector + Lightbox, Destination Router modal, status bar chip, keybinds, command palette actions, and plugin Settings page.

---

## Local Development Setup

### Option A: Symlink / Editable Dev Install into Hermes

Clone your fork and point Hermes at it:

```bash
git clone https://github.com/<your-username>/look-at-this.git
cd look-at-this
```

Or clone directly into `<HERMES_HOME>/plugins/look-at-this` and adopt it so Hermes CLI commands track it:

```bash
hermes plugins adopt look-at-this
hermes plugins enable look-at-this
```

If using Hermes Desktop, toggle **Capabilities → Plugins → Look At This → Desktop** **On** so `desktop/plugin.js` is reconciled into `<HERMES_HOME>/desktop-plugins/look-at-this/`. When iterating on `desktop/plugin.js`, re-enable or reload the plugin (or copy `desktop/plugin.js` to `<HERMES_HOME>/desktop-plugins/look-at-this/plugin.js` and refresh Hermes Desktop).

---

## Testing & Verification

Before opening a pull request, run all local checks from the repository root:

```bash
# 1. Run full unit & integration test suite
python -m unittest discover -s tests -v

# 2. Check Desktop JavaScript syntax
node --check desktop/plugin.js

# 3. Validate Hermes plugin manifest, hooks, security scan, and Desktop SDK rules
hermes plugins validate .
```

If you have Hermes installed locally, also verify runtime diagnostics:

```bash
hermes plugins doctor look-at-this
```

---

## Core Design & Compliance Rules

Every pull request must preserve the plugin's privacy guarantees and Hermes Catalog compatibility:

1. **Zero Self-Updating Code (Hermes Catalog Rule 3)**:
   - Never fetch remote code, run `git pull`, or replace plugin files from inside the plugin. Updates are handled exclusively by Hermes (`hermes plugins update look-at-this`, `/plugins update`, or the Desktop UI).
2. **Public Surfaces & SDK Imports Only (Hermes Catalog Rules 8 & 9)**:
   - Python code must not import private Hermes internals (`hermes_cli._*`, internal modules outside the documented plugin API) or monkeypatch Hermes core classes/routes at runtime.
   - `desktop/plugin.js` must import **only** from `@hermes/plugin-sdk`.
3. **Zero Third-Party Python Dependencies**:
   - Keep `python_dependencies: []` in `plugin.yaml` intact. Capture primitives and PNG encoding (`lat/png_codec.py`) intentionally use the Python standard library and native OS binaries (`ctypes` on Windows, `screencapture`/JXA on macOS, `grim`/`slurp`/`import`/`wmctrl` on Linux).
4. **Privacy & Ephemerality Invariants**:
   - **Pre-capture exclusions**: Password managers (`excluded_apps`) and private/incognito windows (`excluded_title_patterns`) must be refused (`ExcludedWindowError`) **before** any pixel buffer is captured—including region snips and occlusion checks.
   - **Single-turn screenshot lifecycle**: Temporary PNGs in `shots/` must be unlinked via `safe_unlink_shot()` in `finish_turn()`, on replacement, on `/look clear`, and on TTL sweep. Never store screenshot bytes or base64 blobs in Hermes's persistent `state.db`.
   - **Untrusted data boundaries**: Never weaken packet delimiter sanitization (`[look-at-this packet]` neutralization) or move captured screen text outside the untrusted wrapper.
   - **Subagent/Cron isolation**: Automated background turns (`parent_session_id`, `cron`, `webhook`, `[ASYNC DELIVERY`, `[HEARTBEAT`) must never consume user captures.

---

## Bumping the Version

If your PR increments the plugin version, update all **5** version locations consistently (enforced by CI and `tests/test_look_at_this.py`):

1. `plugin.yaml` (`version:`)
2. `skills/look-at-this/SKILL.md` (`version:` in YAML frontmatter)
3. `__init__.py` (`__version__`)
4. `lat/__init__.py` (`__version__`)
5. `dashboard/manifest.json` (`"version"`)

And add an entry under [`CHANGELOG.md`](CHANGELOG.md).

---

## Submitting a Pull Request

1. Fork the repository and create a topic branch (`feat/...` or `fix/...`).
2. Write unit tests in `tests/test_look_at_this.py` covering new behavior or bug fixes (mock platform-specific OS calls when necessary so the suite passes on Windows, macOS, and Linux CI runners).
3. Run all verification checks listed above.
4. Open a PR using the repository PR template and clearly describe what was tested on real hardware vs. mocked.
