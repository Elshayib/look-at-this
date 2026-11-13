"""Comprehensive unit & live host integration test suite for Look At This v2."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lat import capture, hotkey_daemon, models, png_codec, privacy, render, store, surfaces  # noqa: E402


class PngCodecTests(unittest.TestCase):
    def test_encode_crop_downscale_and_dimensions(self) -> None:
        w, h = 64, 48
        # Build non-blank BGRA gradient
        bgra = bytearray(w * h * 4)
        for y in range(h):
            for x in range(w):
                idx = (y * w + x) * 4
                bgra[idx : idx + 4] = bytes((x * 3 % 256, y * 5 % 256, (x + y) % 256, 255))

        self.assertFalse(png_codec.buffer_is_blank(bgra))
        self.assertTrue(png_codec.buffer_is_blank(bytearray(w * h * 4)))

        png_bytes, out_w, out_h = png_codec.encode_bgra_png(bgra, w, h)
        self.assertEqual((out_w, out_h), (w, h))
        self.assertEqual(png_codec.read_png_dimensions(png_bytes), (w, h))

        cropped, cw, ch = png_codec.crop_bgra(bgra, w, h, 10, 8, 20, 16)
        self.assertEqual((cw, ch), (20, 16))
        self.assertEqual(len(cropped), 20 * 16 * 4)

        scaled_png, sw, sh = png_codec.encode_bgra_png(bgra, w, h, max_dim=32)
        self.assertEqual((sw, sh), (32, 24))
        self.assertEqual(png_codec.read_png_dimensions(scaled_png), (32, 24))

        thumb_url = png_codec.bgra_to_thumbnail_data_url(bgra, w, h, max_dim=24)
        self.assertTrue(thumb_url.startswith("data:image/png;base64,"))


class PrivacyAndRedactionTests(unittest.TestCase):
    def test_exclusions_block_password_managers_and_incognito_but_allow_code_titles(self) -> None:
        cfg = models.Settings()
        # Password managers blocked regardless of .exe suffix or casing
        self.assertIsNotNone(privacy.check_window_exclusion("1Password.exe", "Vault", cfg))
        self.assertIsNotNone(privacy.check_window_exclusion("Bitwarden", "My Vault", cfg))
        self.assertIsNotNone(privacy.check_window_exclusion("keepassxc.exe", "Database", cfg))
        # Private / Incognito browser windows blocked
        self.assertIsNotNone(privacy.check_window_exclusion("chrome.exe", "New Tab - Google Chrome (Incognito)", cfg))
        self.assertIsNotNone(privacy.check_window_exclusion("firefox.exe", "Mozilla Firefox Private Browsing", cfg))
        self.assertIsNotNone(privacy.check_window_exclusion("msedge.exe", "InPrivate - Microsoft Edge", cfg))

        # Crucial v2 fix: normal developer windows mentioning "password" or "unlock" are NOT falsely blocked!
        self.assertIsNone(privacy.check_window_exclusion("Code.exe", "password_reset.py - MyProject - Visual Studio Code", cfg))
        self.assertIsNone(privacy.check_window_exclusion("chrome.exe", "Fix unlock modal bug · Issue #42 - Google Chrome", cfg))

        # Malformed user regex fails closed
        bad_cfg = models.Settings(excluded_title_patterns=["[unclosed"])
        self.assertIsNotNone(privacy.check_window_exclusion("Code.exe", "main.py", bad_cfg))

    def test_reversible_redaction_and_detection_counts(self) -> None:
        # Construct test tokens dynamically so static scanners don't treat test fixtures as real keys
        fake_sk = "sk-" + "a" * 24
        fake_gh = "ghp_" + "B" * 28
        raw_cap = models.RawCapture(
            capture_id="pkt123",
            mode="window",
            app="chrome.exe",
            pid=42,
            window_id=100,
            title="Admin alice@example.com portal",
            bounds=models.RegionRect(0, 0, 800, 600),
            url="https://user:hunter2@app.example.com/callback?code=xyz987&tab=home",
            selection=f"OPENAI={fake_sk} GH={fake_gh}",
            context_text="Focused input",
        )
        cfg = models.Settings(default_redact=False)
        now = time.time()
        pkt = models.Packet(
            id=raw_cap.capture_id,
            session_id="s1",
            created_at=now,
            expires_at=now + 300,
            mode="window",
            app=raw_cap.app,
            pid=raw_cap.pid,
            window_id=raw_cap.window_id,
            bounds=raw_cap.bounds,
            raw_title=raw_cap.title,
            raw_url=raw_cap.url,
            raw_selection=raw_cap.selection,
            raw_context_text=raw_cap.context_text,
            raw_tree_markdown="",
            element_count=0,
            tree_truncated=False,
            screenshot_path=None,
            screenshot_width=None,
            screenshot_height=None,
            thumbnail_data_url=None,
            fields=list(models.DEFAULT_FIELDS),
            redact_enabled=False,
        )

        # When redact_enabled is False, raw text is kept, applied is empty, but detected > 0!
        texts_off, applied_off, detected_off = privacy.resolve_packet_texts(pkt)
        self.assertIn("alice@example.com", texts_off["title"] or "")
        self.assertEqual(applied_off, {})
        self.assertGreaterEqual(sum(detected_off.values()), 4)

        # Toggling redact_enabled True masks everything non-destructively!
        pkt.redact_enabled = True
        texts_on, applied_on, _ = privacy.resolve_packet_texts(pkt)
        self.assertNotIn("alice@example.com", texts_on["title"] or "")
        self.assertNotIn(fake_sk, texts_on["selection"] or "")
        self.assertNotIn(fake_gh, texts_on["selection"] or "")
        self.assertNotIn("hunter2", texts_on["url"] or "")
        self.assertNotIn("xyz987", texts_on["url"] or "")
        self.assertIn("tab=home", texts_on["url"] or "")
        self.assertEqual(applied_on, detected_off)

    def test_region_overlap_and_overlay_exclusion_rules(self) -> None:
        from unittest.mock import patch as mock_patch
        from lat.capture import linux_backend, macos_backend, win32_overlay

        excluded = models.WindowInfo(
            window_id=10,
            pid=100,
            app="1Password.exe",
            title="Vault",
            bounds=models.RegionRect(100, 100, 300, 200),
            z_order=0,
            is_excluded=True,
            exclusion_reason="excluded app (1Password.exe)",
        )
        normal = models.WindowInfo(
            window_id=20,
            pid=200,
            app="Code.exe",
            title="editor.py",
            bounds=models.RegionRect(0, 0, 800, 600),
            z_order=1,
            is_excluded=False,
        )
        windows = [excluded, normal]

        # Region whose center (20, 20) is outside 1Password, but corner overlaps 1Password -> MUST refuse!
        partial = models.RegionRect(0, 0, 150, 150)
        self.assertIs(privacy.find_overlapping_excluded_window(partial, windows), excluded)

        # BitBlt fallback for `normal` behind `excluded` must refuse when excluded overlaps normal
        self.assertIs(
            privacy.find_overlapping_excluded_window(normal.bounds, windows, stop_at_window_id=normal.window_id),
            excluded,
        )
        # No excluded window in front of `excluded` itself
        self.assertIsNone(
            privacy.find_overlapping_excluded_window(excluded.bounds, windows, stop_at_window_id=excluded.window_id)
        )

        # Excluded window completely occluded by a larger higher-Z normal window -> allowed!
        occluding_front = models.WindowInfo(
            window_id=30,
            pid=300,
            app="chrome.exe",
            title="Docs",
            bounds=models.RegionRect(50, 50, 450, 350),
            z_order=0,
            is_excluded=False,
        )
        occluded_windows = [occluding_front, excluded]
        self.assertIsNone(privacy.find_overlapping_excluded_window(models.RegionRect(0, 0, 500, 500), occluded_windows))

        # Overlay _window_at must NOT skip excluded windows in front of normal windows
        meta = [excluded.to_dict(), normal.to_dict()]
        hit = win32_overlay._window_at(150, 150, meta)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["window_id"], 10)
        self.assertTrue(hit["is_excluded"])

        # Click on excluded window returns excluded action; drag partially overlapping excluded returns excluded
        click_res = win32_overlay._resolve_snip_release(150, 150, 151, 151, 0, 0, 1920, 1080, meta)
        self.assertEqual(click_res["action"], "excluded")
        drag_res = win32_overlay._resolve_snip_release(0, 0, 150, 150, 0, 0, 1920, 1080, meta)
        self.assertEqual(drag_res["action"], "excluded")
        safe_drag = win32_overlay._resolve_snip_release(0, 0, 80, 80, 0, 0, 1920, 1080, meta)
        self.assertEqual(safe_drag["action"], "region")

        # macOS and Linux region capture enforce exclusions
        cfg = models.Settings()
        with mock_patch.object(macos_backend, "list_windows", return_value=windows):
            with self.assertRaises(models.ExcludedWindowError):
                macos_backend.capture_region_rect(partial, cfg)
        with mock_patch.object(linux_backend, "list_windows", return_value=windows):
            with self.assertRaises(models.ExcludedWindowError):
                linux_backend.capture_region_rect(partial, cfg)


class EphemeralStoreAndLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lat-test-")
        self._prev_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = self._tmp.name
        self.db_path = Path(self._tmp.name) / "plugin-data" / "look-at-this" / "data.db"
        self.store = store.EphemeralStore(db_path=self.db_path)

    def tearDown(self) -> None:
        if self._prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._prev_home
        self._tmp.cleanup()

    def _make_shot(self, name: str = "lat_test1.png") -> Path:
        bgra = bytearray([40, 80, 120, 255] * (16 * 12))
        png_bytes, _, _ = png_codec.encode_bgra_png(bgra, 16, 12)
        shot = store.get_shots_dir() / name
        shot.write_bytes(png_bytes)
        return shot

    def test_unbound_fallback_toggle_consume_and_turn_cleanup(self) -> None:
        shot = self._make_shot("lat_turn1.png")
        cap = models.RawCapture(
            capture_id="turn1",
            mode="window",
            app="Code.exe",
            pid=99,
            window_id=555,
            title="app.tsx - Visual Studio Code",
            bounds=models.RegionRect(10, 20, 1200, 800),
            url=None,
            selection="const x: number = 'bad'",
            context_text="Focused control: Editor",
            screenshot_path=str(shot),
            screenshot_width=16,
            screenshot_height=12,
        )
        cfg = models.Settings()
        # Stored with empty session_id (e.g. fresh chat or global hotkey before chat created)
        self.store.build_and_store(cap, cfg, session_id="")

        # Any session sees the unbound packet
        pending = self.store.get_pending("sess-abc")
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.app_display, "VS Code")

        # Toggle redaction and fields across a SECOND store instance sharing SQLite WAL!
        second_store = store.EphemeralStore(db_path=self.db_path)
        updated = second_store.update_pending("sess-abc", redact_enabled=True, fields=["screenshot", "selection"])
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertTrue(updated.redact_enabled)
        self.assertEqual(updated.fields, ["screenshot", "selection"])

        # Consume for turn: pending is now empty, active_turn_packet holds screenshot for vision middleware
        consumed = self.store.consume_for_turn("sess-abc")
        self.assertIsNotNone(consumed)
        self.assertIsNone(self.store.get_pending("sess-abc"))
        self.assertTrue(shot.is_file())

        # Even across a separate process/store instance, active_turn_packet resolves from active_turns!
        turn_pkt = second_store.active_turn_packet("sess-abc")
        self.assertIsNotNone(turn_pkt)

        # Finish turn -> PNG is deleted from disk immediately!
        second_store.finish_turn("sess-abc")
        self.assertFalse(shot.is_file())
        self.assertIsNone(self.store.active_turn_packet("sess-abc"))

    def test_safe_unlink_refuses_outside_files(self) -> None:
        outside = Path(self._tmp.name) / "keep_me.png"
        outside.write_bytes(b"safe")
        store.safe_unlink_shot(str(outside))
        self.assertTrue(outside.is_file())

    def _store_unbound(self, target_mode: str, capture_id: str = "steal1") -> models.Packet:
        shot = self._make_shot(f"lat_{capture_id}.png")
        cap = models.RawCapture(
            capture_id=capture_id,
            mode="window",
            app="python.exe",
            pid=7,
            window_id=77,
            title="Project Helios Telemetry - Build 9482",
            bounds=models.RegionRect(0, 0, 800, 600),
            url=None,
            selection=None,
            context_text="Banner: AMBER-FALCON",
            screenshot_path=str(shot),
            screenshot_width=16,
            screenshot_height=12,
        )
        return self.store.build_and_store(cap, models.Settings(), session_id="", target_mode=target_mode)

    def _write_state_db(self, rows: dict[str, float]) -> None:
        import sqlite3

        db = Path(self._tmp.name) / "state.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, started_at REAL NOT NULL)")
            conn.executemany("INSERT OR REPLACE INTO sessions(id, started_at) VALUES (?, ?)", list(rows.items()))
            conn.commit()
        finally:
            conn.close()

    def test_new_session_capture_is_not_stolen_by_focused_pre_existing_session(self) -> None:
        pkt = self._store_unbound("new_session")
        # The older chat is focused in Desktop (POST /session) and sends a follow-up turn.
        self.store.set_focused_session("local-older-chat")
        self.assertIsNone(
            self.store.consume_for_turn("local-older-chat", is_first_turn=False, platform="tui")
        )
        # Even the CLI platform no longer bypasses the new-session reservation.
        self.assertIsNone(self.store.consume_for_turn("local-cli-chat", is_first_turn=False, platform="cli"))
        # The existing chat's status shows it only as "queued elsewhere", never as its own attachment.
        self.assertIsNone(self.store.get_pending("local-older-chat"))
        self.assertIsNotNone(self.store.get_unbound_pending())

        # The fresh draft's first turn claims it.
        got = self.store.consume_for_turn("local-fresh-draft", is_first_turn=True, platform="tui")
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.id, pkt.id)
        self.assertIsNotNone(self.store.active_turn_packet("local-fresh-draft"))
        self.store.finish_turn("local-fresh-draft")

    def test_new_session_capture_rejects_first_turn_sessions_created_before_capture(self) -> None:
        pkt = self._store_unbound("new_session", capture_id="steal2")
        self._write_state_db({
            "local-empty-old": pkt.created_at - 600.0,  # an empty chat opened long before the hotkey
            "local-created-after": pkt.created_at + 3.0,
        })
        self.assertIsNotNone(store.lookup_session_started_at("local-empty-old"))
        self.assertIsNone(store.lookup_session_started_at("local-missing"))

        self.assertIsNone(self.store.consume_for_turn("local-empty-old", is_first_turn=True, platform="tui"))
        # A remote messaging platform's brand-new chat must not swallow a desktop capture either.
        self.assertIsNone(self.store.consume_for_turn("tg-new-chat", is_first_turn=True, platform="telegram"))
        got = self.store.consume_for_turn("local-created-after", is_first_turn=True, platform="tui")
        self.assertIsNotNone(got)
        self.store.finish_turn("local-created-after")

    def test_ask_capture_waits_for_explicit_destination(self) -> None:
        pkt = self._store_unbound("ask", capture_id="ask1")
        self.store.set_focused_session("local-older-chat")
        for sid, first in (("local-older-chat", False), ("local-fresh-draft", True), ("cli-x", True)):
            self.assertIsNone(self.store.consume_for_turn(sid, is_first_turn=first, platform="cli"))
        self.assertIsNone(self.store.get_pending("local-older-chat"))
        self.assertIsNotNone(self.store.get_pending(""))

        # User picks "Current session" on an unsent draft -> reserved for the draft's first turn.
        moved = self.store.retarget_pending(packet_id=pkt.id, to_session_id="")
        self.assertIsNotNone(moved)
        assert moved is not None
        self.assertEqual(moved.target_mode, "new_session")
        self.assertIsNone(self.store.consume_for_turn("local-older-chat", is_first_turn=False))
        self.assertIsNotNone(self.store.consume_for_turn("local-fresh-draft", is_first_turn=True))
        self.store.finish_turn("local-fresh-draft")

        # Or the user picks an existing chat -> exact slot, consumed on a non-first turn too.
        pkt2 = self._store_unbound("ask", capture_id="ask2")
        self.store.retarget_pending(packet_id=pkt2.id, to_session_id="local-older-chat")
        self.assertIsNone(self.store.consume_for_turn("local-other", is_first_turn=True))
        got = self.store.consume_for_turn("local-older-chat", is_first_turn=False)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.id, pkt2.id)
        self.store.finish_turn("local-older-chat")

    def test_any_mode_unbound_capture_keeps_first_come_behaviour(self) -> None:
        self._store_unbound("any", capture_id="any1")
        self.assertIsNotNone(self.store.consume_for_turn("local-whoever", is_first_turn=False))
        self.store.finish_turn("local-whoever")


class VisionInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lat-vis-")
        bgra = bytearray([10, 20, 30, 255] * (8 * 8))
        png, _, _ = png_codec.encode_bgra_png(bgra, 8, 8)
        self.shot = Path(self._tmp.name) / "shot.png"
        self.shot.write_bytes(png)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_chat_completions_anthropic_and_responses_formats(self) -> None:
        # 1. OpenAI Chat Completions
        req_oai = {"messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "explain this"}]}
        res_oai = render.inject_ephemeral_image_into_request(
            req_oai, screenshot_path=str(self.shot), api_mode="chat_completions", provider="openrouter", model="google/gemini-2.5-pro"
        )
        self.assertIsNotNone(res_oai)
        assert res_oai is not None
        user_content = res_oai["request"]["messages"][1]["content"]
        self.assertEqual(len(user_content), 2)
        self.assertEqual(user_content[1]["type"], "image_url")
        # Original request is never mutated
        self.assertEqual(req_oai["messages"][1]["content"], "explain this")

        # 2. Anthropic Messages API
        req_ant = {"system": "sys", "messages": [{"role": "user", "content": "what broke?"}]}
        res_ant = render.inject_ephemeral_image_into_request(
            req_ant, screenshot_path=str(self.shot), api_mode="anthropic_messages", provider="anthropic", model="claude-sonnet-4-5"
        )
        self.assertIsNotNone(res_ant)
        assert res_ant is not None
        ant_parts = res_ant["request"]["messages"][0]["content"]
        self.assertEqual(ant_parts[1]["type"], "image")
        self.assertEqual(ant_parts[1]["source"]["media_type"], "image/png")

        # 3. Codex / OpenAI Responses API
        req_resp = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "check ui"}]}]}
        res_resp = render.inject_ephemeral_image_into_request(
            req_resp, screenshot_path=str(self.shot), api_mode="codex_responses", provider="openai-codex", model="gpt-5"
        )
        self.assertIsNotNone(res_resp)
        assert res_resp is not None
        resp_parts = res_resp["request"]["input"][0]["content"]
        self.assertEqual(resp_parts[1]["type"], "input_image")

        # 4. Text-only model is skipped cleanly
        skipped = render.inject_ephemeral_image_into_request(
            req_oai, screenshot_path=str(self.shot), api_mode="chat_completions", provider="deepseek", model="deepseek-chat"
        )
        self.assertIsNone(skipped)


class HotkeyAndSurfaceTests(unittest.TestCase):
    def test_hotkey_chord_parser(self) -> None:
        self.assertIsNone(hotkey_daemon.parse_hotkey_chord(""))
        self.assertIsNone(hotkey_daemon.parse_hotkey_chord("off"))
        mods, vk = hotkey_daemon.parse_hotkey_chord("ctrl+alt+l")  # type: ignore[misc]
        self.assertEqual(mods, 0x0002 | 0x0001)
        self.assertEqual(vk, ord("L"))
        mods2, vk2 = hotkey_daemon.parse_hotkey_chord("alt+shift+f10")  # type: ignore[misc]
        self.assertEqual(mods2, 0x0001 | 0x0004)
        self.assertEqual(vk2, 0x70 + 9)
        with self.assertRaises(ValueError):
            hotkey_daemon.parse_hotkey_chord("l")

    @unittest.skipUnless(sys.platform == "win32", "Live Win32 smoke test runs on Windows host")
    def test_live_windows_capture_hooks_and_api_end_to_end(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from dashboard.plugin_api import router

        with tempfile.TemporaryDirectory(prefix="lat-live-") as tmp:
            prev = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = tmp
            try:
                # 1. Enumerate real visible windows
                out_raw = surfaces.tool_lat_windows({})
                payload = json.loads(out_raw)
                self.assertTrue(payload.get("ok"), out_raw)
                wins = [w for w in payload.get("windows", []) if not w.get("is_excluded")]

                # 2. FastAPI plugin_api routes end-to-end (region capture -> inspect -> patch -> clear)
                app = FastAPI()
                app.include_router(router)
                client = TestClient(app)

                cap_res = client.post(
                    "/capture",
                    json={
                        "session_id": "sess-e2e",
                        "mode": "region",
                        "region": {"x": 20, "y": 20, "width": 200, "height": 140},
                    },
                )
                self.assertEqual(cap_res.status_code, 200, cap_res.text)
                cap_json = cap_res.json()
                self.assertTrue(cap_json["ok"])
                self.assertEqual(cap_json["pending"]["mode"], "region")
                self.assertTrue(cap_json["pending"]["thumbnail_data_url"].startswith("data:image/png;base64,"))

                shot_res = client.get("/screenshot?session_id=sess-e2e").json()
                self.assertTrue(shot_res["ok"])
                self.assertEqual((shot_res["width"], shot_res["height"]), (200, 140))

                patch_res = client.patch(
                    "/pending",
                    json={"session_id": "sess-e2e", "redact_enabled": True, "fields": ["screenshot", "context"]},
                ).json()
                self.assertTrue(patch_res["ok"])
                self.assertTrue(patch_res["pending"]["redact_enabled"])
                self.assertEqual(patch_res["pending"]["fields"], ["screenshot", "context"])

                # 3. Run the real agent turn hooks + vision middleware!
                hook_out = surfaces.hook_pre_llm_call(session_id="sess-e2e", model="google/gemini-2.5-pro", provider="openrouter")
                self.assertIsNotNone(hook_out)
                assert hook_out is not None
                self.assertIn("[look-at-this packet", hook_out["context"])
                self.assertIn("ephemeral image", hook_out["context"])

                mw_out = surfaces.middleware_llm_request(
                    {"messages": [{"role": "user", "content": "explain this"}]},
                    session_id="sess-e2e",
                    provider="openrouter",
                    model="google/gemini-2.5-pro",
                    api_mode="chat_completions",
                )
                self.assertIsNotNone(mw_out)
                assert mw_out is not None
                self.assertEqual(mw_out["request"]["messages"][0]["content"][1]["type"], "image_url")

                # Finish turn -> temporary PNG deleted immediately
                surfaces.hook_post_llm_call(session_id="sess-e2e")
                self.assertEqual(list(store.get_shots_dir().glob("lat_*.png")), [])

                # 4. If a real application window is open, test /look pick -> /look redact -> /look clear
                if wins:
                    first_wid = str(wins[0]["window_id"])
                    surfaces.bind_context(None, lambda _q, _choices: first_wid)
                    look_msg = surfaces.handle_look_command("pick", session_id="sess-pick")
                    self.assertIn("Captured Window:", look_msg)

                    redact_msg = surfaces.handle_look_command("redact on", session_id="sess-pick")
                    self.assertIn("Redaction: ON", redact_msg)

                    clear_msg = surfaces.handle_look_command("clear", session_id="sess-pick")
                    self.assertIn("discarded", clear_msg)
                    self.assertEqual(list(store.get_shots_dir().glob("lat_*.png")), [])
            finally:
                hotkey_daemon.HOTKEYS.stop()
                capture.uia_worker.UIA.close()
                surfaces.bind_context(None, None)
                if prev is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = prev

    def test_gateway_register_starts_and_unloads_hotkeys(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Win32 hotkey registration test only runs on Windows")

        import importlib.util
        import types

        class DummyCtx:
            def __init__(self) -> None:
                self.unload_callbacks: list[Any] = []

            def register_tool(self, **kwargs: Any) -> None:
                pass

            def register_hook(self, *args: Any, **kwargs: Any) -> None:
                pass

            def register_middleware(self, *args: Any, **kwargs: Any) -> None:
                pass

            def register_command(self, *args: Any, **kwargs: Any) -> None:
                pass

            def on_unload(self, cb: Any) -> None:
                self.unload_callbacks.append(cb)

        with tempfile.TemporaryDirectory() as td:
            prev_home = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = td
            try:
                spec = importlib.util.spec_from_file_location(
                    "hermes_plugins_test_lat",
                    ROOT / "__init__.py",
                    submodule_search_locations=[str(ROOT)],
                )
                assert spec and spec.loader
                mod = importlib.util.module_from_spec(spec)
                mod.__package__ = "hermes_plugins_test_lat"
                mod.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
                sys.modules["hermes_plugins_test_lat"] = mod
                spec.loader.exec_module(mod)

                # 1. Outside gateway.run (CLI/validate/doctor), register() must NOT start hotkeys
                sys.modules.pop("gateway.run", None)
                cli_ctx = DummyCtx()
                mod.register(cli_ctx)
                from hermes_plugins_test_lat.lat import hotkey_daemon as mod_hk  # type: ignore[import-not-found]
                from hermes_plugins_test_lat.lat.capture import uia_worker as mod_uia  # type: ignore[import-not-found]

                self.assertFalse(mod_hk.HOTKEYS.status()["running"])

                # 2. Inside gateway.run, register() MUST start hotkeys and wire on_unload cleanup
                sys.modules["gateway.run"] = types.ModuleType("gateway.run")
                gw_ctx = DummyCtx()
                mod.register(gw_ctx)
                st = mod_hk.HOTKEYS.status()
                self.assertTrue(st["running"])
                self.assertTrue(len(gw_ctx.unload_callbacks) >= 1)

                # 3. Unloading stops the hotkey thread and releases locks cleanly
                for cb in reversed(gw_ctx.unload_callbacks):
                    cb()
                self.assertFalse(mod_hk.HOTKEYS.status()["running"])
                mod_uia.UIA.close()
            finally:
                sys.modules.pop("gateway.run", None)
                for k in [k for k in list(sys.modules) if k.startswith("hermes_plugins_test_lat")]:
                    sys.modules.pop(k, None)
                if prev_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = prev_home

    def test_hotkey_defaults_to_new_session_and_protects_against_background_steal(self) -> None:
        from unittest.mock import patch as mock_patch

        with tempfile.TemporaryDirectory(prefix="lat-hk-new-") as td:
            prev_home = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = td
            try:
                shot = store.get_shots_dir() / "lat_hkshot1.png"
                png_bytes, _, _ = png_codec.encode_bgra_png(bytearray([20, 60, 140, 255] * 64), 8, 8)
                shot.write_bytes(png_bytes)

                fake_raw = models.RawCapture(
                    capture_id="hkshot1",
                    mode="window",
                    app="chrome.exe",
                    pid=4242,
                    window_id=9001,
                    title="Nous Portal - Google Chrome",
                    bounds=models.RegionRect(0, 0, 1280, 800),
                    url="https://portal.nousresearch.com/models",
                    selection="gemini-4-carbon",
                    context_text="Focused control: Tab",
                    screenshot_path=str(shot),
                    screenshot_width=8,
                    screenshot_height=8,
                    thumbnail_data_url="data:image/png;base64,AAAA",
                )

                # Simulate an earlier session running a turn first (the exact stale-focus trap)
                surfaces.hook_pre_llm_call(
                    session_id="local-old-session",
                    model="google/gemini-4-carbon",
                    provider="nous",
                    is_first_turn=False,
                )

                emitted: list[tuple[str, dict]] = []
                with (
                    mock_patch.object(capture, "capture_window", return_value=fake_raw),
                    mock_patch.object(capture, "focus_hermes", return_value=True),
                    mock_patch.object(hotkey_daemon, "emit_plugin_event", side_effect=lambda ev, pl=None: emitted.append((ev, pl or {}))),
                ):
                    hotkey_daemon.HOTKEYS._handle_hotkey(hotkey_daemon.ID_WINDOW_HOTKEY)

                cap_events = [pl for ev, pl in emitted if ev == "hotkey.captured"]
                self.assertEqual(len(cap_events), 1)
                self.assertEqual(cap_events[0].get("hotkey_target"), "new_session")
                self.assertEqual(cap_events[0].get("session_id"), "")

                # Subagents, cron turns, and non-focused ongoing background tabs MUST NOT steal the packet!
                self.assertIsNone(
                    surfaces.hook_pre_llm_call(
                        session_id="subagent-123",
                        parent_session_id="local-old-session",
                        is_first_turn=True,
                        model="google/gemini-4-carbon",
                        provider="nous",
                    )
                )
                self.assertIsNone(
                    surfaces.hook_pre_llm_call(
                        session_id="cron-123",
                        platform="cron",
                        is_first_turn=True,
                        model="google/gemini-4-carbon",
                        provider="nous",
                    )
                )
                self.assertIsNone(
                    surfaces.hook_pre_llm_call(
                        session_id="local-background-tab",
                        is_first_turn=False,
                        model="google/gemini-4-carbon",
                        provider="nous",
                    )
                )

                # Now the user sends their first prompt in the newly opened session -> MUST consume & attach vision!
                new_turn = surfaces.hook_pre_llm_call(
                    session_id="local-brand-new-session",
                    is_first_turn=True,
                    model="google/gemini-4-carbon",
                    provider="nous",
                )
                self.assertIsNotNone(new_turn)
                assert new_turn is not None
                self.assertIn("Nous Portal - Google Chrome", new_turn["context"])
                self.assertIn("ephemeral image", new_turn["context"])

                mw = surfaces.middleware_llm_request(
                    {"messages": [{"role": "user", "content": "can you see my screen?"}]},
                    session_id="local-brand-new-session",
                    provider="nous",
                    model="google/gemini-4-carbon",
                    api_mode="chat_completions",
                )
                self.assertIsNotNone(mw)
                assert mw is not None
                self.assertEqual(mw["request"]["messages"][0]["content"][1]["type"], "image_url")

                surfaces.hook_post_llm_call(session_id="local-brand-new-session")
                self.assertFalse(shot.exists())
            finally:
                if prev_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = prev_home

    def test_retarget_and_single_hotkey_instance_across_plugin_api(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import importlib.util
        import types

        with tempfile.TemporaryDirectory(prefix="lat-retarget-") as td:
            prev_home = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = td
            unload_cbs: list = []
            try:
                # Load __init__.py as hermes_plugins.look_at_this AND mount dashboard/plugin_api.py
                pkg_parent = types.ModuleType("hermes_plugins")
                pkg_parent.__path__ = []  # type: ignore[attr-defined]
                sys.modules["hermes_plugins"] = pkg_parent
                sys.modules["gateway.run"] = types.ModuleType("gateway.run")

                spec = importlib.util.spec_from_file_location(
                    "hermes_plugins.look_at_this",
                    ROOT / "__init__.py",
                    submodule_search_locations=[str(ROOT)],
                )
                assert spec and spec.loader
                mod = importlib.util.module_from_spec(spec)
                mod.__package__ = "hermes_plugins.look_at_this"
                mod.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
                sys.modules["hermes_plugins.look_at_this"] = mod
                spec.loader.exec_module(mod)

                unload_cbs = []

                class Ctx:
                    def register_tool(self, **kw): pass
                    def register_hook(self, *a, **kw): pass
                    def register_middleware(self, *a, **kw): pass
                    def register_command(self, *a, **kw): pass
                    def on_unload(self, cb): unload_cbs.append(cb)

                mod.register(Ctx())

                from dashboard.plugin_api import router

                app = FastAPI()
                app.include_router(router)
                client = TestClient(app)

                if sys.platform == "win32":
                    st = client.get("/status?session_id=").json()
                    self.assertTrue(st["ok"])
                    self.assertTrue(st["hotkeys"]["running"], st["hotkeys"])
                    self.assertNotIn(
                        "Another Hermes instance already owns the system-wide hotkeys.",
                        st["hotkeys"].get("warnings", []),
                    )

                # Verify /settings and /hotkey-seq routes
                set_res = client.post("/settings", json={"hotkey_target": "ask"}).json()
                self.assertTrue(set_res["ok"])
                self.assertEqual(set_res["settings"]["hotkey_target"], "ask")
                get_set = client.get("/settings").json()
                self.assertTrue(get_set["ok"])
                self.assertEqual(get_set["settings"]["hotkey_target"], "ask")
                hk_seq = client.get("/hotkey-seq").json()
                self.assertTrue(hk_seq["ok"])
                self.assertIn("seq", hk_seq)

                # Capture unbound (new_session) and verify only_if_new_session guard against older sessions
                cap = client.post(
                    "/capture",
                    json={
                        "session_id": "",
                        "target_mode": "new_session",
                        "mode": "region",
                        "region": {"x": 10, "y": 10, "width": 80, "height": 60},
                    },
                ).json()
                self.assertTrue(cap["ok"])
                pkt_id = cap["pending"]["id"]
                pkt_created_at = float(cap["pending"]["created_at"])

                import sqlite3 as _sqlite3
                _sconn = _sqlite3.connect(str(Path(td) / "state.db"))
                try:
                    _sconn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, started_at REAL NOT NULL)")
                    _sconn.execute("INSERT OR REPLACE INTO sessions(id, started_at) VALUES (?, ?)", ("local-old-chat", pkt_created_at - 120.0))
                    _sconn.execute("INSERT OR REPLACE INTO sessions(id, started_at) VALUES (?, ?)", ("local-brand-new-chat", pkt_created_at + 1.0))
                    _sconn.commit()
                finally:
                    _sconn.close()

                # only_if_new_session=True refuses older session in state.db (404), keeping packet unbound
                refused = client.post(
                    "/retarget",
                    json={
                        "packet_id": pkt_id,
                        "to_session_id": "local-old-chat",
                        "target_mode": "session",
                        "only_if_new_session": True,
                    },
                )
                self.assertEqual(refused.status_code, 404)

                # Existing session sees unbound_pending so UI can offer 1-click "Attach here instead"
                other_st = client.get("/status?session_id=local-existing-chat").json()
                self.assertIsNone(other_st["pending"])
                self.assertIsNotNone(other_st.get("unbound_pending"))

                # only_if_new_session=True succeeds for a session created after the capture
                accepted = client.post(
                    "/retarget",
                    json={
                        "packet_id": pkt_id,
                        "to_session_id": "local-brand-new-chat",
                        "target_mode": "session",
                        "only_if_new_session": True,
                    },
                )
                self.assertEqual(accepted.status_code, 200)
                self.assertEqual(accepted.json()["pending"]["session_id"], "local-brand-new-chat")

                # Explicit retarget (without only_if_new_session) moves to any chosen existing session
                ret = client.post(
                    "/retarget",
                    json={"packet_id": pkt_id, "to_session_id": "local-existing-chat"},
                ).json()
                self.assertTrue(ret["ok"])
                self.assertEqual(ret["pending"]["session_id"], "local-existing-chat")

                # Now local-existing-chat owns pending directly, and unbound is empty
                after_st = client.get("/status?session_id=local-existing-chat").json()
                self.assertIsNotNone(after_st["pending"])
                self.assertEqual(after_st["pending"]["id"], pkt_id)
                self.assertIsNone(client.get("/status?session_id=").json()["pending"])
            finally:
                for cb in reversed(unload_cbs):
                    try:
                        cb()
                    except Exception:
                        pass
                hotkey_daemon.HOTKEYS.stop()
                sys.modules.pop("gateway.run", None)
                for k in [k for k in list(sys.modules) if k == "hermes_plugins" or k.startswith("hermes_plugins.")]:
                    sys.modules.pop(k, None)
                if prev_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = prev_home

    def test_desktop_plugin_contract(self) -> None:
        src = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
        self.assertIn("focusedStoredSessionId", src)
        self.assertIn("session.new", src)
        self.assertIn("/retarget", src)
        self.assertIn("Where should this", src)


if __name__ == "__main__":
    unittest.main()
