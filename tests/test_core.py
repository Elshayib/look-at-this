"""Unit tests for the capture core. No driver, no network."""

from __future__ import annotations

import base64
import json
import math
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lat_core  # noqa: E402
from companion import lat_companion, win_hotkey  # noqa: E402
from companion.lat_companion import PendingBox  # noqa: E402

# A real 1x1 transparent PNG, used by the companion /take tests.
PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)


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


class PacketDictTests(unittest.TestCase):
    def test_from_dict_rejects_another_schema(self):
        with self.assertRaises(ValueError):
            lat_core.Packet.from_dict({"schema": "something-else"})

    def test_foreign_screenshot_path_is_ignored(self):
        data = packet(session_id="").to_dict()
        data["screenshot_path"] = r"C:\Windows\notepad.exe"
        pkt = lat_core.Packet.from_dict(data)
        self.assertIsNone(pkt.screenshot_path)

    def test_non_finite_timestamps_are_replaced(self):
        data = packet(session_id="").to_dict()
        data["expires_at"] = 1e999  # json parses this as +inf
        data["created_at"] = float("inf")
        pkt = lat_core.Packet.from_dict(data)
        self.assertTrue(math.isfinite(pkt.expires_at))
        self.assertTrue(math.isfinite(pkt.created_at))
        self.assertGreater(pkt.expires_at, 0)
        self.assertGreater(pkt.created_at, 0)
        public = pkt.public()
        self.assertGreaterEqual(public["expires_in"], 0)
        self.assertIn("captured_at", public)

        data["created_at"] = float("nan")
        pkt_nan = lat_core.Packet.from_dict(data)
        self.assertTrue(math.isfinite(pkt_nan.created_at))
        self.assertGreater(pkt_nan.created_at, 0)
        self.assertGreaterEqual(pkt_nan.public()["expires_in"], 0)

    def test_absurd_finite_timestamps_are_bounded(self):
        data = packet(session_id="").to_dict()
        data["created_at"] = 1e20
        data["expires_at"] = 1e20
        pkt = lat_core.Packet.from_dict(data)
        self.assertLess(pkt.created_at, 1e12)
        self.assertLess(pkt.expires_at, 1e12)
        self.assertGreaterEqual(pkt.public()["expires_in"], 0)
        self.assertIn("captured", lat_core.render_context(pkt))

    def test_screenshot_must_be_a_png_inside_shots(self):
        data = packet(session_id="").to_dict()
        token = lat_core.data_dir() / "companion.token"
        data["screenshot_path"] = str(token)
        self.assertIsNone(lat_core.Packet.from_dict(data).screenshot_path)

        txt = lat_core.shots_dir() / "lat_not_a_png.txt"
        txt.write_bytes(b"x")
        self.addCleanup(txt.unlink, missing_ok=True)
        data["screenshot_path"] = str(txt)
        self.assertIsNone(lat_core.Packet.from_dict(data).screenshot_path)

    def test_real_png_inside_shots_survives(self):
        png = lat_core.shots_dir() / "lat_shots_only.png"
        png.write_bytes(b"x")
        self.addCleanup(png.unlink, missing_ok=True)
        data = packet(session_id="").to_dict()
        data["screenshot_path"] = str(png)
        pkt = lat_core.Packet.from_dict(data)
        self.assertIsNotNone(pkt.screenshot_path)
        self.assertEqual(Path(pkt.screenshot_path).resolve(), png.resolve())


class MarkerTests(unittest.TestCase):
    """The disk marker must never carry captured text — only routing metadata."""

    def setUp(self):
        lat_core.clear_pending_marker()
        lat_core.active_session_path().unlink(missing_ok=True)

    def tearDown(self):
        lat_core.clear_pending_marker()
        lat_core.active_session_path().unlink(missing_ok=True)

    def test_marker_contains_only_routing_metadata(self):
        pkt = packet(id="mark1", session_id="s1")
        lat_core.write_pending_marker(pkt)
        raw = lat_core.pending_marker_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertIsInstance(data, dict)
        self.assertEqual(set(data), {"id", "session_id", "expires_at"})
        self.assertEqual(data["id"], "mark1")
        self.assertEqual(data["session_id"], "s1")
        self.assertEqual(data["expires_at"], pkt.expires_at)
        for leaked in ("title", "url", "selection", "tree_markdown", "screenshot_path",
                       "app", "pid", "fields", "redactions"):
            self.assertNotIn(leaked, data)
        # Defense in depth: none of the captured text appears in the raw file.
        for secret in (pkt.title, pkt.url, pkt.selection, 'Text "hello"'):
            self.assertNotIn(secret, raw)

    def test_read_returns_the_marker_while_valid(self):
        lat_core.write_pending_marker(packet(id="mark2"))
        data = lat_core.read_pending_marker()
        self.assertIsNotNone(data)
        self.assertEqual(data["id"], "mark2")
        self.assertTrue(lat_core.pending_marker_path().exists())

    def test_expired_marker_returns_none_and_is_deleted(self):
        lat_core.write_pending_marker(packet(expires_at=time.time() - 1))
        path = lat_core.pending_marker_path()
        self.assertTrue(path.exists())
        self.assertIsNone(lat_core.read_pending_marker())
        self.assertFalse(path.exists())

    def test_malformed_marker_returns_none_and_is_deleted(self):
        path = lat_core.pending_marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(lat_core.read_pending_marker())
        self.assertFalse(path.exists())

    def test_clear_pending_marker_removes_it(self):
        path = lat_core.pending_marker_path()
        lat_core.write_pending_marker(packet())
        self.assertTrue(path.exists())
        lat_core.clear_pending_marker()
        self.assertFalse(path.exists())
        self.assertIsNone(lat_core.read_pending_marker())

    def test_clear_pending_marker_respects_expect_id(self):
        lat_core.write_pending_marker(packet(id="A"))
        lat_core.clear_pending_marker("B")
        marker = lat_core.read_pending_marker()
        self.assertIsNotNone(marker)
        self.assertEqual(marker["id"], "A")
        lat_core.clear_pending_marker("A")
        self.assertFalse(lat_core.pending_marker_path().exists())
        self.assertIsNone(lat_core.read_pending_marker())

    def test_non_finite_marker_expiry_returns_none_and_is_deleted(self):
        path = lat_core.pending_marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"id": "x", "expires_at": 1e999}', encoding="utf-8")
        self.assertTrue(path.exists())
        self.assertIsNone(lat_core.read_pending_marker())
        self.assertFalse(path.exists())

    def test_active_session_roundtrip_and_clear(self):
        path = lat_core.active_session_path()
        lat_core.write_active_session("s1")
        self.assertEqual(lat_core.read_active_session(), "s1")
        lat_core.write_active_session("")
        self.assertFalse(path.exists())
        self.assertEqual(lat_core.read_active_session(), "")


class CompanionApiTests(unittest.TestCase):
    def setUp(self):
        lat_core.STORE._by_session.clear()
        lat_core.clear_pending_marker()

    def tearDown(self):
        lat_core.STORE._by_session.clear()
        lat_core.clear_pending_marker()

    def test_pending_packet_prefers_the_local_store(self):
        lat_core.STORE.put(packet())
        cfg = lat_core.Settings()
        with mock.patch.object(lat_core, "companion_pending") as pending:
            got = lat_core.pending_packet("s1", settings=cfg)
        self.assertIsNotNone(got)
        self.assertEqual(got.id, "abc123")
        pending.assert_not_called()

    def test_pending_packet_falls_back_to_the_companion(self):
        remote = packet(id="remote")
        cfg = lat_core.Settings()
        with mock.patch.object(lat_core, "companion_pending", return_value=remote) as pending:
            got = lat_core.pending_packet("s1", settings=cfg)
        self.assertIs(got, remote)
        pending.assert_called_once_with(cfg, "s1")

    def test_companion_take_writes_the_png_and_clears_the_marker(self):
        pkt = packet(id="take1")
        lat_core.write_pending_marker(pkt)
        self.assertTrue(lat_core.pending_marker_path().exists())
        payload = {
            "ok": True,
            "packet": pkt.to_dict(),
            "screenshot_b64": PNG_1X1_B64,
        }
        with mock.patch.object(lat_core, "companion_request", return_value=payload):
            got = lat_core.companion_take(lat_core.Settings(), "s1")
        self.assertIsNotNone(got)
        self.assertEqual(got.id, "take1")
        expected = lat_core.shots_dir() / "lat_take1.png"
        try:
            self.assertEqual(Path(got.screenshot_path), expected)
            self.assertEqual(expected.read_bytes(), base64.b64decode(PNG_1X1_B64))
        finally:
            expected.unlink(missing_ok=True)
        self.assertFalse(lat_core.pending_marker_path().exists())

    def test_companion_take_passes_timeout_and_clears_only_its_own_marker(self):
        cfg = lat_core.Settings()
        pkt = packet(id="take-timeout")
        payload = {"ok": True, "packet": pkt.to_dict()}
        lat_core.write_pending_marker(pkt)
        with mock.patch.object(lat_core, "companion_request", return_value=payload) as req:
            got = lat_core.companion_take(cfg, "s1", timeout=1.25)
        self.assertIsNotNone(got)
        self.assertEqual(got.id, "take-timeout")
        self.assertEqual(req.call_count, 1)
        self.assertEqual(req.call_args.kwargs.get("timeout"), 1.25)
        self.assertEqual(req.call_args.args[1:3], ("POST", "/take"))
        self.assertFalse(lat_core.pending_marker_path().exists())

        # A newer capture's marker must survive an older take's clear.
        lat_core.write_pending_marker(packet(id="newer"))
        with mock.patch.object(lat_core, "companion_request", return_value=payload):
            lat_core.companion_take(cfg, "s1", timeout=1.25)
        marker = lat_core.read_pending_marker()
        self.assertIsNotNone(marker)
        self.assertEqual(marker["id"], "newer")

    def test_set_active_session_posts_the_id_and_reports_success(self):
        cfg = lat_core.Settings()
        with mock.patch.object(lat_core, "companion_request", return_value={"ok": True}) as req:
            self.assertTrue(lat_core.companion_set_active_session(cfg, "s1"))
        req.assert_called_once_with(
            cfg, "POST", "/active-session", {"session_id": "s1"}, timeout=1.5
        )

    def test_set_active_session_returns_false_when_the_companion_fails(self):
        cfg = lat_core.Settings()
        with mock.patch.object(
            lat_core, "companion_request", side_effect=lat_core.LookAtError("down")
        ):
            self.assertFalse(lat_core.companion_set_active_session(cfg, "s1"))

    def test_update_pending_fields_falls_through_to_the_companion(self):
        updated = packet(fields=["url"])
        cfg = lat_core.Settings()
        with mock.patch.object(lat_core, "companion_update_fields", return_value=updated) as upd:
            got = lat_core.update_pending_fields("s1", ["url"], settings=cfg)
        self.assertIs(got, updated)
        upd.assert_called_once_with(cfg, "s1", ["url"])

    def test_clear_pending_reports_the_companion_clear(self):
        cfg = lat_core.Settings()
        with mock.patch.object(lat_core, "companion_clear", return_value=True) as cleared:
            self.assertTrue(lat_core.clear_pending("s1", settings=cfg))
        cleared.assert_called_once_with(cfg, "s1")


class PendingBoxTests(unittest.TestCase):
    """The companion's in-memory single-packet box (no disk text, PNG to TTL)."""

    def _png(self, name: str) -> str:
        path = lat_core.shots_dir() / name
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        self.addCleanup(path.unlink, missing_ok=True)
        return str(path)

    def test_put_get_take_is_single_use(self):
        box = PendingBox()
        box.put(packet(id="one"))
        self.assertFalse(box.is_empty())
        self.assertEqual(box.get("s1").id, "one")
        self.assertEqual(box.take("s1").id, "one")
        self.assertIsNone(box.take("s1"))
        self.assertIsNone(box.get("s1"))
        self.assertTrue(box.is_empty())

    def test_session_binding(self):
        box = PendingBox()
        box.put(packet(session_id="s1"))
        self.assertIsNone(box.get(""))
        self.assertIsNone(box.get("s2"))
        self.assertIsNone(box.take(""))
        self.assertEqual(box.get("s1").id, "abc123")

        box.take("s1")
        box.put(packet(id="unbound", session_id=""))
        self.assertEqual(box.get("anything").id, "unbound")
        self.assertEqual(box.take("whatever").id, "unbound")

    def test_expired_packet_is_dropped(self):
        shot = self._png("lat_box_expired_get.png")
        box = PendingBox()
        box.put(packet(expires_at=time.time() - 1, screenshot_path=shot))
        self.assertIsNone(box.get("s1"))
        self.assertTrue(box.is_empty())
        self.assertFalse(Path(shot).exists())

        shot2 = self._png("lat_box_expired_take.png")
        box.put(packet(expires_at=time.time() - 1, screenshot_path=shot2))
        self.assertIsNone(box.take("s1"))
        self.assertTrue(box.is_empty())
        self.assertFalse(Path(shot2).exists())

    def test_replacement_returns_the_new_packet_and_deletes_the_old_png(self):
        shot = self._png("lat_box_first.png")
        box = PendingBox()
        box.put(packet(id="first", screenshot_path=shot))
        box.put(packet(id="second"))
        self.assertEqual(box.get("s1").id, "second")
        self.assertFalse(Path(shot).exists())

    def test_update_fields_filters_and_drops_the_png(self):
        shot = self._png("lat_box_fields.png")
        box = PendingBox()
        box.put(packet(screenshot_path=shot))
        got = box.update_fields("s1", ["url", "not-a-field"])
        self.assertIsNotNone(got)
        self.assertEqual(got.fields, ["url"])
        self.assertIsNone(got.screenshot_path)
        self.assertFalse(Path(shot).exists())
        self.assertIsNone(box.update_fields("s2", ["url"]))
        self.assertIsNone(box.update_fields("", ["url"]))

    def test_clear_deletes_the_png_and_reports_once(self):
        shot = self._png("lat_box_clear.png")
        box = PendingBox()
        box.put(packet(screenshot_path=shot))
        self.assertTrue(box.clear("s1"))
        self.assertFalse(Path(shot).exists())
        self.assertFalse(box.clear("s1"))

    def test_clear_with_expect_id_keeps_a_foreign_packet(self):
        shot = self._png("lat_box_expect.png")
        box = PendingBox()
        box.put(packet(id="mine", screenshot_path=shot))
        self.assertFalse(box.clear("s1", expect_id="other"))
        self.assertIsNotNone(box.get("s1"))
        self.assertTrue(Path(shot).exists())
        self.assertTrue(box.clear("s1", expect_id="mine"))
        self.assertIsNone(box.get("s1"))
        self.assertFalse(Path(shot).exists())


class TokenTests(unittest.TestCase):
    """A blank --token must never become the bearer secret."""

    def test_whitespace_explicit_token_falls_back_to_the_token_file(self):
        path = lat_companion.token_path()
        before = path.read_bytes() if path.exists() else None
        stored = before.decode("utf-8").strip() if before is not None else ""
        try:
            token = lat_companion.load_or_create_token("   ")
            self.assertTrue(token)
            self.assertEqual(token, token.strip())
            self.assertTrue(path.exists())
            on_disk = path.read_text(encoding="utf-8").strip()
            self.assertTrue(on_disk)
            self.assertEqual(token, on_disk)
            if stored:
                self.assertEqual(token, stored)  # reused, not regenerated
                self.assertEqual(path.read_bytes(), before)  # file untouched
        finally:
            if before is None:
                path.unlink(missing_ok=True)
            elif path.read_bytes() != before:
                path.write_bytes(before)


class CompanionChordTests(unittest.TestCase):
    def test_ctrl_alt_letter(self):
        mods, vk = win_hotkey.parse_chord("ctrl+alt+l")
        self.assertEqual(mods, 0x0002 | 0x0001)
        self.assertEqual(vk, ord("L"))

    def test_space(self):
        mods, vk = win_hotkey.parse_chord("ctrl+shift+space")
        self.assertEqual(mods, 0x0002 | 0x0004)
        self.assertEqual(vk, 0x20)

    def test_function_key(self):
        mods, vk = win_hotkey.parse_chord("f5")
        self.assertEqual(mods, 0)
        self.assertEqual(vk, 0x74)

    def test_bad_chord(self):
        with self.assertRaises(ValueError):
            win_hotkey.parse_chord("hyper+l")


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
