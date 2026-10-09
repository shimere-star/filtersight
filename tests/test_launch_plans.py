import asyncio
import hashlib
import tempfile
import time
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
    def _member_app(self, screen=None, session_state=None):
        profile_response = Mock()
        profile_response.raise_for_status.return_value = None
        profile_response.json.return_value = {
            "email": "member@example.com",
            "plan": "filtersight",
            "has_chat": True,
            "cancel_at_period_end": False,
        }
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20)
        app.query_params["view"] = "member"
        app.session_state["member_access_token"] = "token"
        if screen:
            app.session_state["checkin_screen"] = screen
        for key, value in (session_state or {}).items():
            app.session_state[key] = value
        with patch("requests.get", return_value=profile_response):
            app.run()
        return app, profile_response

    def test_member_dashboard_starts_structured_check_in(self):
        """Catches shipping the companion without the required check-in entry."""
        profile_response = Mock()
        profile_response.raise_for_status.return_value = None
        profile_response.json.return_value = {
            "email": "member@example.com",
            "plan": "filtersight",
            "has_chat": True,
            "cancel_at_period_end": False,
        }
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20)
        app.query_params["view"] = "member"
        app.session_state["member_access_token"] = "token"

        with patch("requests.get", return_value=profile_response):
            app.run()

        self.assertEqual(list(app.exception), [])
        self.assertIn("Check in", [button.label for button in app.button])

        with patch("requests.get", return_value=profile_response):
            next(button for button in app.button if button.label == "Check in").click().run()

        self.assertIn("Check in", [title.value for title in app.title])
        self.assertEqual(app.radio[0].label, "What's happening right now?")
        self.assertEqual(
            app.radio[0].options,
            [
                "I'm feeling a craving or temptation right now.",
                "I'm feeling stressed or anxious.",
                "I'm bored and looking for something to do.",
                "I'm feeling lonely.",
                "I hit a block by accident — I wasn't trying to access anything.",
                "I need access to a blocked site for a legitimate reason.",
                "Something else is going on.",
            ],
        )

    def test_intervention_menu_renders_all_required_choices(self):
        """Catches omitting a Phase 1 intervention from the member flow."""
        app, _ = self._member_app("interventions")
        labels = [button.label for button in app.button]

        for title in (
            "60-second breathing reset",
            "Two-minute grounding",
            "Cold water reset",
            "Step outside",
            "Tidy one thing",
            "Five-minute cooldown",
            "Ten-minute cooldown",
            "Talk it through",
        ):
            self.assertTrue(any(label.startswith(title) for label in labels), title)
        self.assertIn("I need help now", labels)

    def test_emergency_screen_has_crisis_copy_and_real_988_link(self):
        """Catches an emergency route that omits immediate human support."""
        app, _ = self._member_app("emergency")
        visible = " ".join(
            [item.value for item in app.markdown]
            + [item.value for item in app.error]
        )

        self.assertIn("not emergency or professional mental-health services", visible)
        self.assertIn("local emergency services now", visible)
        self.assertIn("call or text **988**", visible)
        self.assertIn('href="tel:988"', visible)
        self.assertIn("Back to check-in", [button.label for button in app.button])

    def test_checkin_features_create_no_database_tables(self):
        """Catches adding persistent storage for temporary check-in activity."""
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "customers.db")
            with patch.object(webhook_server, "DB_PATH", db_path):
                db = webhook_server.get_db()
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                db.close()

        self.assertEqual(
            tables,
            {"customers", "member_magic_links", "member_sessions"},
        )

    def test_signup_offers_exactly_one_filtersight_plan(self):
        """Catches restoring tier selection instead of the single public plan."""
        app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=20)

        self.assertEqual(list(app.exception), [])
        self.assertEqual(len(app.radio), 0)
        self.assertEqual([item.value for item in app.subheader], ["FilterSight"])
        visible = " ".join(item.value for item in app.markdown).lower()
        self.assertIn("$10/month", visible)
        self.assertNotIn("tier 1", visible)
        self.assertNotIn("tier 2", visible)
        self.assertNotIn("filter + companion", visible)

    def test_checkout_uses_single_current_plan_metadata(self):
        """Catches provisioning a checkout with retired tier metadata."""
        checkout = types.SimpleNamespace(url="https://checkout.example/test")
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20)
        with (
            patch.dict(
                "os.environ",
                {"STRIPE_SECRET_KEY": "sk_test_123", "STRIPE_PRICE_ID": "price_10"},
                clear=False,
            ),
            patch("stripe.checkout.Session.create", return_value=checkout) as create,
        ):
            app.run()
            app.text_input[0].set_value("member@example.com")
            app.button[0].click().run()

        self.assertEqual(list(app.exception), [])
        self.assertEqual(create.call_args.kwargs["metadata"], {"plan": "filtersight"})
        self.assertEqual(
            create.call_args.kwargs["subscription_data"],
            {"metadata": {"plan": "filtersight"}},
        )

    def test_cooldown_renders_as_temporary_session_timer(self):
        """Catches a cooldown that fails to render or claims persistence."""
        with patch(
            "streamlit.fragment",
            side_effect=lambda **_kwargs: lambda function: function,
        ):
            app, _ = self._member_app(
                "exercise", {"checkin_exercise": "cooldown_5"}
            )

        self.assertEqual(list(app.exception), [])
        visible = " ".join(item.value for item in app.markdown)
        self.assertIn("current session only", visible)
        self.assertIn("isn't saved", visible)
        self.assertIn("Stop", [button.label for button in app.button])

    def test_emergency_detour_clears_active_cooldown_timer(self):
        """Catches a later cooldown inheriting an earlier timer's start time."""
        with patch(
            "streamlit.fragment",
            side_effect=lambda **_kwargs: lambda function: function,
        ):
            app, profile_response = self._member_app(
                "exercise",
                {
                    "checkin_exercise": "cooldown_5",
                    "checkin_timer_started": time.monotonic(),
                },
            )
            with patch("requests.get", return_value=profile_response):
                next(
                    button for button in app.button
                    if button.label == "I need help now"
                ).click().run()

        self.assertNotIn("checkin_timer_started", app.session_state)
        self.assertIn("If you need help right now", [title.value for title in app.title])

    def test_public_pricing_describes_one_plan_without_legacy_names(self):
        """Catches stale public pricing that still exposes retired plans."""
        parser = TextCollector()
        parser.feed((ROOT / "index.html").read_text())
        page_text = " ".join(parser.parts).lower()

        self.assertIn("filtersight", page_text)
        self.assertIn("$10", page_text)
        self.assertNotIn("filter + companion", page_text)
        self.assertNotIn("$5", page_text)
        self.assertNotIn("tier 1", page_text)
        self.assertNotIn("tier 2", page_text)
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

    def test_retired_tier_checkout_metadata_is_rejected(self):
        """Catches retired tier metadata being provisioned as the current plan."""
        metadata = Mock()
        metadata.to_dict.return_value = {"plan": "tier1"}
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
        ):
            with self.assertRaises(HTTPException) as raised:
                webhook_server.verify_paid_checkout("cs_123")

        self.assertEqual(raised.exception.status_code, 403)

    def test_filtersight_profile_keeps_dns_query_logging_disabled(self):
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
                "member@example.com", "filtersight", "sub_123"
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
                "member@example.com", "filtersight", "sub_123"
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
                    "member@example.com", "filtersight", "sub_123"
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
                    "member@example.com", "filtersight", "sub_123"
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

    def test_every_current_subscriber_has_companion_access(self):
        """Catches reintroducing tier-based feature entitlements."""
        subscription = types.SimpleNamespace(cancel_at_period_end=False, current_period_end=123)

        def profile_for(plan):
            member = {
                "email": "member@example.com",
                "plan": plan,
                "subscription": subscription,
            }
            with patch.object(webhook_server, "require_member_session", return_value=member):
                return asyncio.run(webhook_server.member_profile(Mock()))

        profile = profile_for("filtersight")
        self.assertEqual(profile["plan"], "filtersight")
        self.assertTrue(profile["has_chat"])

    def test_member_session_rejects_a_retired_plan_record(self):
        """Catches legacy database rows inheriting current-plan entitlement."""
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "customers.db")
            token = "member-token"
            future = "2099-01-01T00:00:00+00:00"
            with patch.object(webhook_server, "DB_PATH", db_path):
                db = webhook_server.get_db()
                db.execute(
                    "INSERT INTO customers (email, stripe_subscription_id, active, plan) VALUES (?, ?, 1, ?)",
                    ("member@example.com", "sub_legacy", "tier1"),
                )
                db.execute(
                    "INSERT INTO member_sessions (token_hash, email, stripe_subscription_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
                    (webhook_server.hash_token(token), "member@example.com", "sub_legacy", future, future),
                )
                db.commit()
                db.close()
                request = Mock(headers={"Authorization": f"Bearer {token}"})
                with self.assertRaises(HTTPException) as raised:
                    webhook_server.require_member_session(request)

        self.assertEqual(raised.exception.status_code, 403)

    def test_followup_actions_match_the_approved_copy(self):
        """Catches a required safe exit disappearing from a follow-up state."""
        from companion_flow import FOLLOWUP_OPTIONS, FOLLOWUP_RESPONSES

        self.assertEqual(
            FOLLOWUP_RESPONSES[FOLLOWUP_OPTIONS[2]][1],
            ["Emergency support", "Talk with companion", "Done"],
        )

    def test_ending_chat_shows_completion_before_returning(self):
        """Catches dropping the specified chat completion message."""
        app, profile_response = self._member_app(
            "chat",
            {"member_chat_history": [{"role": "assistant", "content": "I'm here. What's on your mind?"}]},
        )
        with patch("requests.get", return_value=profile_response):
            next(button for button in app.button if button.label == "End conversation").click().run()

        visible = " ".join(item.value for item in app.markdown)
        self.assertIn("Thanks for talking it through. You can come back anytime.", visible)
        self.assertIn("Done", [button.label for button in app.button])


if __name__ == "__main__":
    unittest.main()
