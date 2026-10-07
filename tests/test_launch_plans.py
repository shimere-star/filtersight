import asyncio
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
