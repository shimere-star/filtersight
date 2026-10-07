import asyncio
import hashlib
import tempfile
import types
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from streamlit.testing.v1 import AppTest

import webhook_server


ROOT = Path(__file__).resolve().parents[1]


class TextCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


class LaunchPlanTests(unittest.TestCase):
    def test_signup_offers_exactly_filter_and_companion(self):
        """Catches accidentally hiding Tier 2 or restoring the retired Tier 3."""
        app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=20)

        self.assertEqual(list(app.exception), [])
        self.assertEqual(
            app.radio[0].options,
            ["Filter — $5/mo", "Filter + Companion — $10/mo"],
        )

    def test_public_pricing_describes_two_plans_without_messaging(self):
        """Catches a stale public offer that still advertises Tier 3 or texting."""
        parser = TextCollector()
        parser.feed((ROOT / "index.html").read_text())
        page_text = " ".join(parser.parts).lower()

        self.assertIn("filter + companion", page_text)
        self.assertNotIn("complete", page_text)
        self.assertNotIn("sms", page_text)
        self.assertNotIn("encouragement texts", page_text)

    def test_backend_exposes_no_messaging_routes(self):
        """Catches leaving a callable messaging endpoint after removing Twilio."""
        paths = {route.path for route in webhook_server.app.routes}

        self.assertNotIn("/sms-webhook", paths)
        self.assertNotIn("/save-contact", paths)
        self.assertNotIn("/notify-attempt", paths)
        self.assertNotIn("/poll-nextdns-and-notify", paths)
        self.assertNotIn("/check-for-removed-profiles", paths)

    def test_tier3_checkout_is_rejected_even_if_old_flag_is_enabled(self):
        """Catches legacy configuration accidentally re-enabling retired Tier 3."""
        metadata = Mock()
        metadata.to_dict.return_value = {"tier": "tier3"}
        session = types.SimpleNamespace(
            status="complete",
            payment_status="paid",
            subscription="sub_123",
            metadata=metadata,
            customer_details=types.SimpleNamespace(email="member@example.com"),
            customer_email=None,
            customer="cus_123",
        )
        subscription = types.SimpleNamespace(status="active")

        with (
            patch.object(webhook_server.stripe.checkout.Session, "retrieve", return_value=session),
            patch.object(webhook_server.stripe.Subscription, "retrieve", return_value=subscription),
            patch.object(webhook_server.stripe, "api_key", "sk_test_123"),
            patch.object(webhook_server, "ENABLE_TIER2_TIER3", True, create=True),
        ):
            with self.assertRaises(HTTPException) as raised:
                webhook_server.verify_paid_checkout("cs_123")

        self.assertEqual(raised.exception.status_code, 403)

    def test_tier2_profile_keeps_dns_query_logging_disabled(self):
        """Catches collecting browsing logs that the companion does not need."""
        template = Mock()
        template.json.return_value = {"data": {"settings": {"logs": {"enabled": True}}}}
        template.raise_for_status.return_value = None
        created = Mock()
        created.json.return_value = {"data": {"id": "profile_123"}}
        created.raise_for_status.return_value = None

        with (
            patch.object(webhook_server, "NEXTDNS_API_KEY", "key"),
            patch.object(webhook_server, "NEXTDNS_PROFILE_ID", "template"),
            patch.object(webhook_server.requests, "get", return_value=template),
            patch.object(webhook_server.requests, "post", return_value=created) as post,
        ):
            profile_id = webhook_server.create_nextdns_profile(
                "member@example.com", "tier2", "sub_123"
            )

        self.assertEqual(profile_id, "profile_123")
        self.assertFalse(post.call_args.kwargs["json"]["settings"]["logs"]["enabled"])

    def test_reused_profile_disables_and_clears_existing_logs(self):
        """Catches reusing an older profile while its query logging stays enabled."""
        profile_name = f"Filtersight {hashlib.sha256(b'sub_123').hexdigest()[:12]}"
        existing = Mock(ok=True)
        existing.json.return_value = {
            "data": [{"id": "profile_existing", "name": profile_name}]
        }
        existing.raise_for_status.return_value = None
        updated = Mock(ok=True)
        updated.raise_for_status.return_value = None
        cleared = Mock(ok=True)
        cleared.raise_for_status.return_value = None

        with (
            patch.object(webhook_server, "NEXTDNS_API_KEY", "key"),
            patch.object(webhook_server, "NEXTDNS_PROFILE_ID", "template"),
            patch.object(webhook_server.requests, "get", return_value=existing),
            patch.object(webhook_server.requests, "patch", return_value=updated) as patch_request,
            patch.object(webhook_server.requests, "delete", return_value=cleared) as delete_request,
        ):
            profile_id = webhook_server.create_nextdns_profile(
                "member@example.com", "tier2", "sub_123"
            )

        self.assertEqual(profile_id, "profile_existing")
        patch_request.assert_called_once_with(
            "https://api.nextdns.io/profiles/profile_existing/settings/logs",
            headers={"X-Api-Key": "key", "Content-Type": "application/json"},
            json={"enabled": False},
            timeout=30,
        )
        delete_request.assert_called_once_with(
            "https://api.nextdns.io/profiles/profile_existing/logs",
            headers={"X-Api-Key": "key"},
            timeout=30,
        )

    def test_reused_profile_rejects_nextdns_200_error_response(self):
        """Catches treating a NextDNS HTTP 200 error body as privacy success."""
        profile_name = f"Filtersight {hashlib.sha256(b'sub_123').hexdigest()[:12]}"
        existing = Mock(ok=True)
        existing.json.return_value = {
            "data": [{"id": "profile_existing", "name": profile_name}]
        }
        existing.raise_for_status.return_value = None
        rejected_update = Mock(ok=True)
        rejected_update.json.return_value = {
            "errors": [{"code": "invalid", "detail": "logs update rejected"}]
        }
        rejected_update.raise_for_status.return_value = None

        with (
            patch.object(webhook_server, "NEXTDNS_API_KEY", "key"),
            patch.object(webhook_server, "NEXTDNS_PROFILE_ID", "template"),
            patch.object(webhook_server.requests, "get", return_value=existing),
            patch.object(webhook_server.requests, "patch", return_value=rejected_update),
            patch.object(webhook_server.requests, "delete") as delete_request,
        ):
            with self.assertRaises(HTTPException) as raised:
                webhook_server.create_nextdns_profile(
                    "member@example.com", "tier2", "sub_123"
                )

        self.assertEqual(raised.exception.status_code, 502)
        delete_request.assert_not_called()

    def test_reused_profile_rejects_log_clear_200_error_response(self):
        """Catches treating a rejected stored-log deletion as privacy success."""
        profile_name = f"Filtersight {hashlib.sha256(b'sub_123').hexdigest()[:12]}"
        existing = Mock(ok=True)
        existing.json.return_value = {
            "data": [{"id": "profile_existing", "name": profile_name}]
        }
        existing.raise_for_status.return_value = None
        updated = Mock(ok=True)
        updated.json.return_value = {"data": {}}
        updated.raise_for_status.return_value = None
        rejected_clear = Mock(ok=True)
        rejected_clear.json.return_value = {
            "errors": [{"code": "invalid", "detail": "log deletion rejected"}]
        }
        rejected_clear.raise_for_status.return_value = None

        with (
            patch.object(webhook_server, "NEXTDNS_API_KEY", "key"),
            patch.object(webhook_server, "NEXTDNS_PROFILE_ID", "template"),
            patch.object(webhook_server.requests, "get", return_value=existing),
            patch.object(webhook_server.requests, "patch", return_value=updated),
            patch.object(webhook_server.requests, "delete", return_value=rejected_clear),
        ):
            with self.assertRaises(HTTPException) as raised:
                webhook_server.create_nextdns_profile(
                    "member@example.com", "tier2", "sub_123"
                )

        self.assertEqual(raised.exception.status_code, 502)

    def test_database_migration_purges_legacy_messaging_data(self):
        """Catches retaining phone numbers and consent state after SMS removal."""
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "customers.db")
            with patch.object(webhook_server, "DB_PATH", db_path):
                db = webhook_server.get_db()
                for column in (
                    "user_phone TEXT",
                    "accountability_phone TEXT",
                    "user_sms_opted_in INTEGER DEFAULT 0",
                    "user_sms_consent_at TEXT",
                    "user_sms_consent_version TEXT",
                    "accountability_sms_opted_in INTEGER DEFAULT 0",
                    "partner_opt_in_status TEXT",
                    "partner_opt_in_confirmed_at TEXT",
                ):
                    db.execute(f"ALTER TABLE customers ADD COLUMN {column}")
                db.execute(
                    """INSERT INTO customers (
                           email, user_phone, accountability_phone,
                           user_sms_opted_in, user_sms_consent_at,
                           user_sms_consent_version, accountability_sms_opted_in,
                           partner_opt_in_status, partner_opt_in_confirmed_at
                       ) VALUES (?, ?, ?, 1, ?, ?, 1, ?, ?)""",
                    (
                        "member@example.com",
                        "+15551234567",
                        "+15557654321",
                        "2026-09-25T00:00:00Z",
                        "2026-09-25-v1",
                        "confirmed",
                        "2026-09-25T00:01:00Z",
                    ),
                )
                db.commit()
                db.close()

                migrated = webhook_server.get_db()
                row = migrated.execute(
                    """SELECT user_phone, accountability_phone,
                              user_sms_opted_in, user_sms_consent_at,
                              user_sms_consent_version, accountability_sms_opted_in,
                              partner_opt_in_status, partner_opt_in_confirmed_at
                       FROM customers WHERE email = ?""",
                    ("member@example.com",),
                ).fetchone()
                migrated.close()

        self.assertEqual(row, (None, None, 0, None, None, 0, None, None))

    def test_member_companion_is_only_for_tier2(self):
        """Catches granting retired Tier 3 the launch companion entitlement."""
        subscription = types.SimpleNamespace(cancel_at_period_end=False, current_period_end=123)

        def profile_for(tier):
            member = {
                "email": "member@example.com",
                "tier": tier,
                "subscription": subscription,
            }
            with patch.object(webhook_server, "require_member_session", return_value=member):
                return asyncio.run(webhook_server.member_profile(Mock()))

        self.assertTrue(profile_for("tier2")["has_chat"])
        self.assertFalse(profile_for("tier1")["has_chat"])
        self.assertFalse(profile_for("tier3")["has_chat"])


if __name__ == "__main__":
    unittest.main()
