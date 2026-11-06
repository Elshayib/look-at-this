"""Unit tests for the capture core. No driver, no network."""

from __future__ import annotations

import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lat_core  # noqa: E402


def window(app="chrome.exe", title="Example — Chrome", z=5, pid=100, width=1200, height=800,
           on_screen=True, window_id=1):
    return {
        "app_name": app, "title": title, "z_index": z, "pid": pid,
        "window_id": window_id, "is_on_screen": on_screen,
        "bounds": {"x": 0, "y": 0, "width": width, "height": height},
    }


def packet(**over) -> lat_core.Packet:
    now = time.time()
    base = dict(
        id="abc123", session_id="s1", created_at=now, expires_at=now + 300,
        app="chrome.exe", pid=1, window_id=1, title="Example — Chrome",
        url="https://example.com/a", url_source="uia-address-bar",
        selection="hello", selection_source="uia-textpattern",
        screenshot_path=None, screenshot_width=None, screenshot_height=None,
        tree_markdown='- Window "Example"\n    - [0] Text "hello"',
        element_count=3, returned_count=3, truncated=False,
        fields=list(lat_core.ALL_FIELDS), redactions={},
    )
    base.update(over)
    return lat_core.Packet(**base)


class RedactionTests(unittest.TestCase):
    def test_email_and_key(self):
        counts: dict[str, int] = {}
        out = lat_core.redact_text("mail a.b@example.com key sk-abcdefghijklmnop1234", counts)
        self.assertIn("[REDACTED:email]", out)
        self.assertIn("[REDACTED:openai_key]", out)
        self.assertEqual(counts["email"], 1)

    def test_card_uses_luhn(self):
        counts: dict[str, int] = {}
        self.assertIn("[REDACTED:card_number]", lat_core.redact_text("card 4242 4242 4242 4242", counts))
        counts2: dict[str, int] = {}
        self.assertNotIn("REDACTED", lat_core.redact_text("order 1234 5678 9012 3456", counts2))

    def test_url_query_secret_is_removed_but_path_kept(self):
        counts: dict[str, int] = {}
        out = lat_core.redact_url("https://x.test/callback?code=abc123&token=zzz&page=2", counts)
        self.assertIn("/callback", out)
        self.assertIn("page=2", out)
        self.assertNotIn("abc123", out)
        self.assertNotIn("zzz", out)
        self.assertEqual(counts["url_query"], 2)

    def test_url_userinfo_is_removed(self):
        counts: dict[str, int] = {}
        out = lat_core.redact_url("https://user:pass@example.com/x", counts)
        self.assertEqual(out, "https://example.com/x")
        self.assertEqual(counts["url_userinfo"], 1)

    def test_private_key_block(self):
        counts: dict[str, int] = {}
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"
        out = lat_core.redact_text(text, counts)
        self.assertIn("[REDACTED:private_key]", out)
        self.assertNotIn("MIIE", out)


class StoreTests(unittest.TestCase):
    def setUp(self):
        lat_core.STORE._by_session.clear()

    def test_put_get_and_consume_once(self):
        lat_core.STORE.put(packet())
        self.assertIsNotNone(lat_core.STORE.get("s1"))
        self.assertIsNotNone(lat_core.STORE.consume("s1"))
        self.assertIsNone(lat_core.STORE.get("s1"))

    def test_expired_packet_is_not_returned(self):
        lat_core.STORE.put(packet(expires_at=time.time() - 1))
        self.assertIsNone(lat_core.STORE.get("s1"))

    def test_replacing_a_session_deletes_the_old_screenshot(self):
        path = lat_core.shots_dir() / "lat_test_old.png"
        path.write_bytes(b"x")
        lat_core.STORE.put(packet(id="old", screenshot_path=str(path)))
        lat_core.STORE.put(packet(id="new"))
        self.assertFalse(path.exists())

    def test_clear_reports_whether_anything_was_pending(self):
        self.assertFalse(lat_core.STORE.clear("s1"))
        lat_core.STORE.put(packet())
        self.assertTrue(lat_core.STORE.clear("s1"))

    def test_unbound_packet_is_consumed_by_a_brand_new_chat(self):
        lat_core.STORE.put(packet(session_id=""))
        got = lat_core.STORE.consume("brand-new-session")
        self.assertIsNotNone(got)
        self.assertIsNone(lat_core.STORE.get("brand-new-session"))

    def test_session_packet_wins_over_the_unbound_slot(self):
        lat_core.STORE.put(packet(id="unbound", session_id=""))
        lat_core.STORE.put(packet(id="specific", session_id="s1"))
        got = lat_core.STORE.consume("s1")
        self.assertEqual(got.id, "specific")
        self.assertIsNotNone(lat_core.STORE.get(""))


class ContextTests(unittest.TestCase):
    def test_context_labels_data_as_untrusted(self):
        text = lat_core.render_context(packet())
        self.assertIn("UNTRUSTED REFERENCE DATA", text)
        self.assertIn("Application: chrome.exe", text)
        self.assertIn("https://example.com/a", text)

    def test_disabled_fields_are_absent(self):
        text = lat_core.render_context(packet(fields=["url"]))
        self.assertIn("URL:", text)
        self.assertNotIn("Accessibility tree", text)
        self.assertNotIn("Screenshot:", text)

    def test_context_is_capped(self):
        text = lat_core.render_context(packet(tree_markdown="x" * 40_000), max_chars=5_000)
        self.assertLessEqual(len(text), 5_100)
        self.assertIn("packet truncated", text)

    def test_selection_preview_is_clipped_in_public_view(self):
        public = packet(selection="y" * 400).public()
        self.assertLessEqual(len(public["selection_preview"]), 160)


class TargetTests(unittest.TestCase):
    def test_hint_matches_app(self):
        cfg = lat_core.Settings()
        target = lat_core.pick_target([window(), window(app="notepad.exe", title="t.txt - Notepad")],
                                      cfg, hint="notepad")
        self.assertEqual(target["app_name"], "notepad.exe")

    def test_excluded_app_refuses_even_when_explicitly_requested(self):
        cfg = lat_core.Settings()
        with self.assertRaises(lat_core.ExcludedWindow):
            lat_core.pick_target([window(app="1Password.exe", title="1Password")], cfg, hint="1password")

    def test_excluded_title_pattern_refuses(self):
        cfg = lat_core.Settings()
        with self.assertRaises(lat_core.ExcludedWindow):
            lat_core.pick_target([window(title="New Incognito Tab - Chrome")], cfg, hint="incognito")

    def test_own_overlay_is_never_the_target(self):
        cfg = lat_core.Settings()
        wins = [window(app="cua-driver.exe", title="Cua.AgentCursorOverlay.default", z=9),
                window(app="notepad.exe", title="t.txt - Notepad", z=2)]
        with mock.patch.object(lat_core, "foreground_hwnd", return_value=None):
            self.assertEqual(lat_core.pick_target(wins, cfg)["app_name"], "notepad.exe")

    def test_all_windows_ignored_raises(self):
        cfg = lat_core.Settings()
        with self.assertRaises(lat_core.CaptureFailed):
            lat_core.pick_target([window(app="hermes.exe", title="Hermes", z=9)], cfg)

    def test_lock_screen_is_never_the_target(self):
        cfg = lat_core.Settings()
        with self.assertRaises(lat_core.CaptureFailed):
            lat_core.pick_target([window(app="LockApp.exe", title="Windows Default Lock Screen", z=9)], cfg)


class CompanionTests(unittest.TestCase):
    def test_payload_roundtrip_writes_the_png(self):
        import base64

        raw = b"\x89PNG\r\n\x1a\n" + b"0" * 32
        payload = {"ok": True, "packet": {
            "app": "notepad.exe", "pid": 7, "title": "t.txt - Notepad",
            "url": None, "url_source": None, "selection": "hello",
            "selection_source": "uia-textpattern", "tree_markdown": '- Window "t"',
            "element_count": 2, "returned_count": 2, "truncated": False,
            "screenshot_b64": base64.b64encode(raw).decode("ascii"),
            "screenshot_width": 100, "screenshot_height": 50,
        }}
        cap = lat_core.capture_from_payload(payload)
        try:
            self.assertEqual(cap.app, "notepad.exe")
            self.assertEqual(cap.selection, "hello")
            self.assertEqual(cap.screenshot_width, 100)
            self.assertTrue(Path(cap.screenshot_path).exists())
            self.assertEqual(Path(cap.screenshot_path).read_bytes(), raw)
        finally:
            Path(cap.screenshot_path).unlink(missing_ok=True)

    def test_payload_without_a_packet_is_refused(self):
        with self.assertRaises(lat_core.CaptureFailed):
            lat_core.capture_from_payload({"ok": True})

    def test_cleartext_http_only_for_local_or_private(self):
        self.assertTrue(lat_core._plain_http_allowed("http://127.0.0.1:7391"))
        self.assertTrue(lat_core._plain_http_allowed("http://192.168.1.10:7391"))
        self.assertTrue(lat_core._plain_http_allowed("https://capture.example.com"))
        self.assertFalse(lat_core._plain_http_allowed("http://capture.example.com"))
        self.assertFalse(lat_core._plain_http_allowed("ftp://127.0.0.1"))


class SpoolTests(unittest.TestCase):
    def setUp(self):
        for path in lat_core.spool_dir().glob("*.json"):
            path.unlink(missing_ok=True)

    def test_roundtrip_is_single_use(self):
        pkt = packet(session_id="", id="spool1")
        lat_core.spool_packet(pkt)
        got = lat_core.take_spooled()
        self.assertIsNotNone(got)
        self.assertEqual(got.id, "spool1")
        self.assertEqual(got.app, "chrome.exe")
        self.assertIsNone(lat_core.take_spooled())

    def test_expired_spool_is_dropped(self):
        lat_core.spool_packet(packet(session_id="", expires_at=time.time() - 1))
        self.assertIsNone(lat_core.take_spooled())

    def test_new_capture_replaces_the_previous_spool(self):
        lat_core.spool_packet(packet(session_id="", id="first"))
        lat_core.spool_packet(packet(session_id="", id="second"))
        self.assertEqual(len(list(lat_core.spool_dir().glob("*.json"))), 1)
        self.assertEqual(lat_core.take_spooled().id, "second")

    def test_from_dict_rejects_another_schema(self):
        with self.assertRaises(ValueError):
            lat_core.Packet.from_dict({"schema": "something-else"})

    def test_foreign_screenshot_path_is_ignored(self):
        data = packet(session_id="").to_dict()
        data["screenshot_path"] = r"C:\Windows\notepad.exe"
        pkt = lat_core.Packet.from_dict(data)
        self.assertIsNone(pkt.screenshot_path)


class CompanionChordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "lat_companion_under_test", ROOT / "companion" / "lat_companion.py")
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_ctrl_alt_letter(self):
        mods, vk = self.mod.parse_chord("ctrl+alt+l")
        self.assertEqual(mods, 0x0002 | 0x0001)
        self.assertEqual(vk, ord("L"))

    def test_space(self):
        mods, vk = self.mod.parse_chord("ctrl+shift+space")
        self.assertEqual(mods, 0x0002 | 0x0004)
        self.assertEqual(vk, 0x20)

    def test_bad_chord(self):
        with self.assertRaises(ValueError):
            self.mod.parse_chord("hyper+l")


class ManifestTests(unittest.TestCase):
    def test_tool_names_do_not_collide_with_builtin_namespaces(self):
        text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        names = re.findall(r"^  - (lat_\w+)$", text, re.MULTILINE)
        self.assertEqual(sorted(names), ["lat_clear", "lat_grab", "lat_status"])
        for name in names:
            self.assertFalse(name.startswith(("browser_", "tab_", "computer_use_")))

    def test_requires_hermes_is_a_semver_floor(self):
        text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        match = re.search(r'^requires_hermes:\s*"([^"]+)"', text, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertRegex(match.group(1), r"^>=\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
