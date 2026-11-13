# Look At This (v2)

Seamless, strictly ephemeral screen, window, and region context for **Hermes Agent**.

Press a system-wide shortcut from **any application**, pick from live thumbnails of open windows, drag a precision screen-region rectangle, or type `/look` in any surface — **Look At This** attaches a crisp screenshot plus compact smart UI context (active browser URL, selected text, focused control, visible UI/error text, and optional accessibility tree) to your next message.

Nothing is captured until you ask. Screenshots are injected **ephemerally** into the model turn (never stored in `state.db` session history) and deleted from disk the moment the turn finishes.

---

## Highlights in v2 (Complete Rewrite)

- **Instant Native Capture (<100 ms on Windows 11)** — Uses per-thread DPI-aware Win32 `PrintWindow(PW_RENDERFULLCONTENT)` + `DwmGetWindowAttribute` + a warm UIAutomation reader and pure-stdlib PNG encoder. Zero `cua-driver` requirement, zero cold-start lag, and automatic filtering of invisible DWM-cloaked UWP shell windows.
- **Three Capture Modes**:
  1. **Active Window (`Ctrl+Alt+L`)** — Captures the foreground window you are working in (or the window directly beneath Hermes), flashes a brief non-activating top-center HUD confirmation pill (`hotkey_hud_feedback`), and brings Hermes Desktop straight to focus.
  2. **Interactive Snip & Window-Hover Overlay (`Ctrl+Alt+S`)** — Freezes a hardware-accelerated GDI canvas across all monitors: click any highlighted window to capture it cleanly, or drag a crosshair rectangle to snip any custom screen region.
  3. **Interactive Window Picker (`Ctrl+Alt+W` / `+ → Pick a window to attach…` / `/look pick`)** — Searchable grid of open windows with live preview thumbnails.
- **Configurable Hotkey Destination (`hotkey_target`)**:
  - `new_session` *(default)* — Automatically opens a fresh Hermes session with the capture attached so hotkey grabs never interrupt or leak into an existing chat.
  - `ask` — Opens the **"Where should this screen capture go?"** modal so you can pick **New session**, **Current chat**, or any recent session (plus a 1-click default switcher).
  - `current_session` — Attaches directly to whichever chat is currently open.
- **Ephemeral Direct Vision + Text Packet** — Vision-capable models (Gemini, Claude, GPT-4o/5, etc.) receive the captured image directly on that turn via `llm_request` middleware without saving multi-megabyte base64 blobs into your chat history; text-only models automatically receive a local fallback path for `vision_analyze`.
- **Zero-Polling Real-Time Desktop UI** — Driven by Hermes's `broadcast_plugin_event` WebSocket stream and WAL-mode `plugin_db` storage:
  - **`Screen Attached` Composer Card** (`COMPOSER_AREAS.top`) with live 1-second TTL countdown, thumbnail preview, **`+ New session · Move...`** destination router, per-field toggle pills (`Image`, `Smart UI`, `Full Tree`, `URL`, `Selection`), one-click reversible **Redact** toggle (showing how many tokens/emails were detected!), and a full-resolution **Lightbox + Packet Inspector** modal.
  - **Composer `+` Menu**, **Status-Bar Trigger**, **Command Palette (`⌘K / Ctrl+K`)**, **Rebindable Keybinds**, and **Settings → Plugins** overview page.
- **Smart Privacy Defaults** — Refuses actual password managers (1Password, Bitwarden, KeePassXC, ProtonPass, etc.) and private/incognito browser windows before any screenshot is taken — without falsely blocking code editors or docs whose window title happens to contain words like `"password"`.

---

## Usage Across Surfaces

| Surface | How to Use |
|---|---|
| **Anywhere on OS (Windows)** | Press **`Ctrl+Alt+L`** to grab the active window or **`Ctrl+Alt+S`** to snip a region / click a window — Hermes Desktop comes straight to focus with your capture queued. |
| **Hermes Desktop** | Click **`Look`** in the bottom status bar, click **`+`** in the composer, press **`Ctrl/Cmd+Alt+L`** / **`S`** / **`W`**, or open the Command Palette (`Ctrl/Cmd+K`). |
| **CLI / TUI / Messaging** | Run `/look`, `/look pick`, `/look snip`, `/look app=chrome`, `/look redact`, `/look tree`, `/look status`, or `/look clear`. |
| **Agent Tools** | Ask *"what does this error on my screen mean?"* — the agent can call `lat_grab` or `lat_windows` on demand. |

---

## Installation

```bash
hermes plugins install https://github.com/Elshayib/look-at-this
hermes plugins enable look-at-this
```

In **Hermes Desktop**, open **Capabilities → Plugins → Look At This** and toggle **Desktop** **On** once (unified git-linked plugin installs default the Desktop surface to `Off` / `defaultEnabled: false`). Configure hotkey routing (`hotkey_target`), shortcuts, HUD feedback, TTL, vision delivery, and privacy rules anytime under **Settings → Plugins → Look At This**.

---

## Platform Matrix

| Platform | Window & Region Capture | Smart Context (URL / Selection / Tree) | System-Wide Global Hotkeys |
|---|---|---|---|
| **Windows 10 / 11** | Built-in Win32 `PrintWindow` / `BitBlt` + hardware GDI Snip Overlay (multi-monitor Per-Monitor-V2 DPI aware) | Warm UIAutomation session + Win32 child fallback | Built-in (`RegisterHotKey`, zero external daemon) |
| **macOS** | `screencapture` + JXA (`System Events`; requires Screen Recording + Accessibility TCC grants) | AppleScript active browser tab URL | In-app Desktop shortcuts (`Cmd+Alt+L` / `S` / `W`) + `/look` |
| **Linux (X11 / Wayland)** | `grim` + `slurp` (Wayland) or `import` + `wmctrl` (X11) | `wl-paste --primary` / `xclip -o` primary selection | In-app Desktop shortcuts (`Ctrl+Alt+L` / `S` / `W`) + `/look` |

---

## Disclosure (Catalog Rule 13)

- **User-initiated capture only.** Nothing is captured continuously or in the background; capture runs only when triggered by a hotkey, UI action, `/look`, or explicit `lat_grab` tool call.
- **What is read.** Target window title, process executable name, window bounds, PNG screenshot (window or selected screen region), active browser address-bar URL, selected text, focused control metadata, and (when enabled) accessibility tree elements. Password managers and private/incognito browser windows are refused **before** any pixel capture occurs.
- **Local storage & ephemerality.** Queued packet metadata is held transiently in `<HERMES_HOME>/plugin-data/look-at-this/data.db` (SQLite WAL, mode `0700`) and temporary PNGs in `<HERMES_HOME>/plugin-data/look-at-this/shots/lat_<id>.png`. Packets expire after `ttl_seconds` (default `300`s), are consumed by a single turn, are never persisted to `state.db` chat transcripts, and their PNGs are deleted immediately upon turn completion (`post_llm_call` / `on_session_end`), `/look clear`, or TTL sweep.
- **Subprocesses & threads.** Zero third-party Python dependencies (`python_dependencies: []`). On Windows, starts one lightweight in-process `RegisterHotKey` listener thread inside `hermes serve` (guarded by `hotkey.lock`), one headless PowerShell UIAutomation reader on demand, and a short-lived Python child process for the interactive GDI snipping overlay. On macOS/Linux, invokes standard OS utilities (`osascript`, `screencapture`, `wmctrl`, `grim`, `slurp`, `import`).
- **Network & credentials.** Zero outbound network requests, zero telemetry, and zero stored credentials or API keys.

---

## License

MIT
