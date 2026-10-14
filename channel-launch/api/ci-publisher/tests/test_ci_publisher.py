"""Tests for the parts that are easy to get quietly wrong.

Run from the ci-publisher directory:

    python3 -m unittest discover -s tests -v

Nothing here touches the network or needs a real credential.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ci_publisher import store
from ci_publisher.auth import _abbreviate, _extract_code, complete_manual_auth
from ci_publisher.config import load_client_config
from ci_publisher.errors import AuthError, ConfigError, QuotaExceeded, ScheduleError
from ci_publisher.quota import COSTS, QuotaLedger, quota_day
from ci_publisher.youtube import parse_publish_at

INSTALLED_CLIENT = {
    "installed": {
        "client_id": "123-abc.apps.googleusercontent.com",
        "client_secret": "test-secret-not-real",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

WEB_CLIENT = {
    "web": {
        "client_id": "456-def.apps.googleusercontent.com",
        "client_secret": "test-secret-not-real",
    }
}


class TempHomeTest(unittest.TestCase):
    """Isolates CI_PUBLISHER_HOME so tests never touch a real token."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("CI_PUBLISHER_HOME")
        os.environ["CI_PUBLISHER_HOME"] = self._tmp.name
        self.home = Path(self._tmp.name)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("CI_PUBLISHER_HOME", None)
        else:
            os.environ["CI_PUBLISHER_HOME"] = self._prev
        self._tmp.cleanup()


class QuotaLedgerTest(TempHomeTest):
    def ledger(self, budget=10_000):
        return QuotaLedger(path=self.home / "quota.json", budget=budget)

    def test_fresh_ledger_has_full_budget(self):
        ledger = self.ledger()
        self.assertEqual(ledger.used(), 0)
        self.assertEqual(ledger.remaining(), 10_000)
        self.assertEqual(ledger.summary()["uploads_left"], 6)

    def test_spend_debits_and_persists(self):
        ledger = self.ledger()
        ledger.spend("videos.insert")
        self.assertEqual(ledger.used(), 1600)
        # A second ledger over the same file sees the same tally.
        self.assertEqual(self.ledger().used(), 1600)

    def test_six_uploads_fit_and_the_seventh_is_refused(self):
        ledger = self.ledger()
        for _ in range(6):
            ledger.spend("videos.insert")
        self.assertEqual(ledger.used(), 9600)
        with self.assertRaises(QuotaExceeded):
            ledger.spend("videos.insert")
        # The refusal must not debit: 400 units are still there for reads.
        self.assertEqual(ledger.used(), 9600)
        self.assertEqual(ledger.remaining(), 400)

    def test_cheap_calls_still_work_when_an_upload_will_not_fit(self):
        ledger = self.ledger()
        for _ in range(6):
            ledger.spend("videos.insert")
        ledger.spend("channels.list")
        self.assertEqual(ledger.used(), 9601)

    def test_unknown_operation_is_refused_rather_than_guessed(self):
        with self.assertRaises(QuotaExceeded):
            self.ledger().spend("videos.teleport")

    def test_refund_credits_back(self):
        ledger = self.ledger()
        ledger.spend("thumbnails.set")
        ledger.refund("thumbnails.set")
        self.assertEqual(ledger.used(), 0)

    def test_refund_never_goes_negative(self):
        ledger = self.ledger()
        ledger.refund("videos.insert")
        self.assertEqual(ledger.used(), 0)

    def test_days_are_tallied_separately(self):
        ledger = self.ledger()
        ledger.spend("videos.insert")
        other_day = "1999-01-01"
        self.assertEqual(ledger.used(day=other_day), 0)
        self.assertEqual(ledger.remaining(day=other_day), 10_000)

    def test_quota_day_follows_pacific_not_utc(self):
        # 06:00 UTC is still the previous day in Pacific time.
        moment = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(quota_day(moment), "2026-10-13")

    def test_corrupt_ledger_file_does_not_crash(self):
        path = self.home / "quota.json"
        path.write_text("{ not json")
        ledger = QuotaLedger(path=path)
        self.assertEqual(ledger.used(), 0)
        ledger.spend("channels.list")
        self.assertEqual(ledger.used(), COSTS["channels.list"])


class PublishAtTest(unittest.TestCase):
    def future(self, **kwargs):
        return datetime.now(timezone.utc) + timedelta(**kwargs)

    def test_future_slot_is_normalised_to_utc_z(self):
        slot = self.future(days=2).replace(microsecond=0)
        result = parse_publish_at(slot.isoformat())
        self.assertTrue(result.endswith("Z"))
        self.assertEqual(datetime.fromisoformat(result.replace("Z", "+00:00")), slot)

    def test_offset_is_converted_to_utc(self):
        slot = self.future(days=3).replace(microsecond=0)
        local = slot.astimezone(timezone(timedelta(hours=1)))
        result = parse_publish_at(local.isoformat())
        self.assertEqual(datetime.fromisoformat(result.replace("Z", "+00:00")), slot)

    def test_naive_timestamp_is_read_as_utc(self):
        slot = self.future(days=4).replace(microsecond=0, tzinfo=None)
        result = parse_publish_at(slot.isoformat())
        self.assertEqual(
            datetime.fromisoformat(result.replace("Z", "+00:00")),
            slot.replace(tzinfo=timezone.utc),
        )

    def test_past_slot_is_refused(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with self.assertRaises(ScheduleError):
            parse_publish_at(past)

    def test_past_slot_shifts_to_the_future_when_asked(self):
        past = datetime.now(timezone.utc) - timedelta(days=2, hours=1)
        result = parse_publish_at(past.isoformat(), allow_shift=True)
        shifted = datetime.fromisoformat(result.replace("Z", "+00:00"))
        self.assertGreater(shifted, datetime.now(timezone.utc))
        # Same clock time, moved forward by whole days.
        self.assertEqual((shifted - past).total_seconds() % 86400, 0)

    def test_garbage_is_rejected_with_a_usable_message(self):
        with self.assertRaises(ScheduleError) as caught:
            parse_publish_at("next tuesday")
        self.assertIn("ISO-8601", str(caught.exception))


class ClientConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, payload):
        path = self.dir / name
        path.write_text(json.dumps(payload))
        return path

    def test_desktop_client_is_accepted(self):
        path = self.write("client_secret.json", INSTALLED_CLIENT)
        _, client_id, resolved = load_client_config(path)
        self.assertEqual(client_id, INSTALLED_CLIENT["installed"]["client_id"])
        self.assertEqual(resolved, path)

    def test_web_client_is_rejected_with_the_reason(self):
        path = self.write("client_secret.json", WEB_CLIENT)
        with self.assertRaises(ConfigError) as caught:
            load_client_config(path)
        message = str(caught.exception)
        self.assertIn("Desktop app", message)
        self.assertIn("force-ssl", message)

    def test_client_without_an_id_is_rejected(self):
        path = self.write("client_secret.json", {"installed": {}})
        with self.assertRaises(ConfigError):
            load_client_config(path)

    def test_unparseable_file_is_rejected(self):
        path = self.dir / "client_secret.json"
        path.write_text("{ not json")
        with self.assertRaises(ConfigError):
            load_client_config(path)


class TokenStoreTest(TempHomeTest):
    def test_roundtrip_records_the_issuing_client(self):
        store.save_token(
            "youtube",
            {"refresh_token": "not-a-real-token"},
            "123-abc.apps.googleusercontent.com",
            ["https://www.googleapis.com/auth/youtube.upload"],
        )
        envelope = store.load_token("youtube")
        self.assertEqual(envelope["client_id"], "123-abc.apps.googleusercontent.com")
        self.assertEqual(envelope["platform"], "youtube")
        self.assertIn("saved_at", envelope)

    def test_token_file_is_not_world_readable(self):
        path = store.save_token("youtube", {"refresh_token": "x"}, "cid", [])
        self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_missing_token_reads_as_none(self):
        self.assertIsNone(store.load_token("youtube"))

    def test_forget_is_idempotent(self):
        store.save_token("youtube", {"refresh_token": "x"}, "cid", [])
        self.assertTrue(store.forget_token("youtube"))
        self.assertFalse(store.forget_token("youtube"))


class ExtractCodeTest(unittest.TestCase):
    def test_full_redirect_url(self):
        url = "http://localhost:8765/?state=xyz&code=4/0AbcDEF&scope=openid"
        self.assertEqual(_extract_code(url), "4/0AbcDEF")

    def test_bare_code_passes_through(self):
        self.assertEqual(_extract_code("4/0AbcDEF"), "4/0AbcDEF")

    def test_query_fragment_without_scheme(self):
        self.assertEqual(_extract_code("?code=4/0Abc&state=1"), "4/0Abc")

    def test_access_denied_explains_the_test_user_fix(self):
        with self.assertRaises(AuthError) as caught:
            _extract_code("http://localhost:8765/?error=access_denied")
        self.assertIn("test user", str(caught.exception))

    def test_empty_input_is_rejected(self):
        with self.assertRaises(AuthError):
            _extract_code("   ")

    def test_url_without_a_code_is_rejected(self):
        with self.assertRaises(AuthError):
            _extract_code("http://localhost:8765/?state=only")


class PendingAuthTest(TempHomeTest):
    """The split consent flow: URL generated by one process, code exchanged by another."""

    def test_pending_roundtrip_preserves_the_code_verifier(self):
        store.save_pending(
            {
                "platform": "youtube",
                "client_id": "123-abc.apps.googleusercontent.com",
                "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
                "redirect_uri": "http://localhost:8765/",
                "state": "abc123",
                "code_verifier": "verifier-value",
            }
        )
        pending = store.load_pending()
        self.assertEqual(pending["code_verifier"], "verifier-value")
        self.assertEqual(pending["redirect_uri"], "http://localhost:8765/")
        self.assertIn("created_at", pending)

    def test_pending_file_is_not_world_readable(self):
        path = store.save_pending({"platform": "youtube"})
        self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_clear_pending_is_idempotent(self):
        store.save_pending({"platform": "youtube"})
        store.clear_pending()
        self.assertIsNone(store.load_pending())
        store.clear_pending()  # must not raise

    def test_completing_with_nothing_pending_explains_how_to_start(self):
        with self.assertRaises(AuthError) as caught:
            complete_manual_auth("youtube", "http://localhost:8765/?code=x")
        self.assertIn("--print-url", str(caught.exception))

    def test_completing_the_wrong_platform_is_refused(self):
        store.save_pending({"platform": "tiktok", "client_id": "cid"})
        with self.assertRaises(AuthError):
            complete_manual_auth("youtube", "http://localhost:8765/?code=x")

    def test_corrupt_pending_file_reads_as_none(self):
        store.pending_path().write_text("{ not json")
        self.assertIsNone(store.load_pending())


class AbbreviateTest(unittest.TestCase):
    def test_keeps_the_distinguishing_prefix(self):
        full = "100000000000-abcdefghijklmnopqrstuvwxyz012345.apps.googleusercontent.com"
        short = _abbreviate(full)
        self.assertTrue(short.startswith("100000000000-"))
        self.assertNotIn("googleusercontent", short)

    def test_two_clients_remain_distinguishable(self):
        a = _abbreviate("111-aaa.apps.googleusercontent.com")
        b = _abbreviate("222-bbb.apps.googleusercontent.com")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
