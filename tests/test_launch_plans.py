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


def app_route_paths(app):
    """Return paths from normal and FastAPI 0.143+ included-router entries."""
    paths = set()

    def visit(routes):
        for route in routes:
            path = getattr(route, "path", None)
            if path is not None:
                paths.add(path)
            included = getattr(route, "original_router", None)
            if included is not None:
                visit(included.routes)

    visit(app.routes)
    return paths


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

    def _journal_app(self, *, profile=None, entries=None, next_cursor=None):
        member_profile = Mock()
        member_profile.raise_for_status.return_value = None
        member_profile.json.return_value = {
            "email": "member@example.com",
            "plan": "filtersight",
            "has_chat": True,
            "cancel_at_period_end": False,
        }
        journal_profile = Mock()
        journal_profile.raise_for_status.return_value = None
        journal_profile.json.return_value = profile or {
            "goal": None,
            "why_it_matters": None,
            "coping_actions": [],
            "updated_at": None,
        }
        journal_entries = Mock()
        journal_entries.raise_for_status.return_value = None
        journal_entries.json.return_value = {
            "entries": entries or [],
            "limit": 50,
            "next_cursor": next_cursor,
        }

        def get(url, **_kwargs):
            if url.endswith("/member/profile"):
                return member_profile
            if url.endswith("/member/journal/profile"):
                return journal_profile
            if url.endswith("/member/journal/entries"):
                return journal_entries
            raise AssertionError(f"Unexpected GET: {url}")

        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20)
        app.query_params["view"] = "member"
        app.session_state["member_access_token"] = "token"
        app.session_state["member_screen"] = "journal"
        with patch("requests.get", side_effect=get):
            app.run()
        return app, get

    def test_private_journal_loads_additional_entry_pages(self):
        """Catches silently hiding older entries after the first API page."""
        first_entry = {
            "id": "entry-newer",
            "created_at": "2026-10-09T21:00:00.000000Z",
            "updated_at": "2026-10-09T21:00:00.000000Z",
            "category": "stress_or_anxiety",
            "action": None,
            "note": "Newer entry",
        }
        older_entry = {
            "id": "entry-older",
            "created_at": "2026-10-08T21:00:00.000000Z",
            "updated_at": "2026-10-08T21:00:00.000000Z",
            "category": "boredom",
            "action": None,
            "note": "Older entry",
        }
        app, get = self._journal_app(entries=[first_entry], next_cursor="cursor-2")
        self.assertIn("Load more entries", [button.label for button in app.button])

        next_page = Mock()
        next_page.raise_for_status.return_value = None
        next_page.json.return_value = {
            "entries": [older_entry],
            "limit": 50,
            "next_cursor": None,
        }

        def paged_get(url, **kwargs):
            if url.endswith("/member/journal/entries") and kwargs.get("params", {}).get("cursor"):
                return next_page
            return get(url, **kwargs)

        with patch("requests.get", side_effect=paged_get) as request_get:
            next(button for button in app.button if button.label == "Load more entries").click().run()

        entries_call = next(
            call
            for call in request_get.call_args_list
            if call.args[0].endswith("/member/journal/entries")
            and call.kwargs.get("params", {}).get("cursor")
        )
        self.assertEqual(entries_call.kwargs["params"], {"limit": 50, "cursor": "cursor-2"})
        visible = " ".join(item.value for item in app.markdown)
        self.assertIn("Newer entry", visible)
        self.assertIn("Older entry", visible)
        self.assertNotIn("Load more entries", [button.label for button in app.button])

    def test_private_journal_workspace_renders_all_primary_tasks(self):
        """Catches shipping the backend without a usable member-facing workspace."""
        app, _ = self._journal_app()

        self.assertEqual(list(app.exception), [])
        self.assertIn("Private journal", [title.value for title in app.title])
        subheaders = [heading.value for heading in app.subheader]
        self.assertIn("Your plan", subheaders)
        self.assertIn("New journal entry", subheaders)
        self.assertIn("Your entries", subheaders)
        self.assertIn("Your journal data", subheaders)
        labels = [button.label for button in app.button]
        self.assertIn("Back to dashboard", labels)
        self.assertIn("Save plan", labels)
        self.assertIn("Save entry", labels)
        self.assertIn("Prepare JSON export", labels)
        self.assertIn("Delete all journal data", labels)
        visible = " ".join(item.value for item in app.markdown)
        self.assertIn("encrypted before it is stored", visible)
        self.assertIn("No journal entries yet", visible)

    def test_member_dashboard_opens_private_journal(self):
        """Catches a journal backend that members cannot reach from the dashboard."""
        app, profile_response = self._member_app()

        self.assertIn("Open private journal", [button.label for button in app.button])
        with patch("requests.get", return_value=profile_response):
            next(button for button in app.button if button.label == "Open private journal").click().run()

        self.assertIn("Private journal", [title.value for title in app.title])

    def test_private_journal_saves_profile_and_note_only_entry(self):
        """Catches losing optional-field semantics between the UI and API."""
        app, get = self._journal_app()

        next(field for field in app.text_input if field.label == "Personal goal").set_value(
            "Make room for the life I want"
        )
        next(field for field in app.text_area if field.label == "Why it matters").set_value(
            "I want to be present."
        )
        next(field for field in app.text_input if field.label == "Coping action 1").set_value(
            "Walk outside"
        )
        profile_response = Mock()
        profile_response.raise_for_status.return_value = None
        profile_response.json.return_value = {}
        with patch("requests.get", side_effect=get), patch(
            "requests.patch", return_value=profile_response
        ) as update_profile:
            next(button for button in app.button if button.label == "Save plan").click().run()

        self.assertEqual(
            update_profile.call_args.kwargs["json"],
            {
                "goal": "Make room for the life I want",
                "why_it_matters": "I want to be present.",
                "coping_actions": ["Walk outside"],
            },
        )

        next(area for area in app.text_area if area.label == "Private note (optional)").set_value(
            "The urge passed after a pause."
        )
        entry_response = Mock()
        entry_response.raise_for_status.return_value = None
        entry_response.json.return_value = {"id": "entry-1"}
        with patch("requests.get", side_effect=get), patch(
            "requests.post", return_value=entry_response
        ) as create_entry:
            next(button for button in app.button if button.label == "Save entry").click().run()

        self.assertEqual(
            create_entry.call_args.kwargs["json"],
            {
                "category": None,
                "action": None,
                "note": "The urge passed after a pause.",
            },
        )

    def test_private_journal_exports_and_requires_delete_all_confirmation(self):
        """Catches an unusable export or an unguarded destructive action."""
        app, get = self._journal_app()
        delete_all = next(button for button in app.button if button.label == "Delete all journal data")
        self.assertTrue(delete_all.disabled)

        export_response = Mock()
        export_response.raise_for_status.return_value = None
        export_response.json.return_value = {
            "profile": {"goal": "Steady progress"},
            "entries": [],
        }

        def export_get(url, **kwargs):
            if url.endswith("/member/journal/export"):
                return export_response
            return get(url, **kwargs)

        with patch("requests.get", side_effect=export_get):
            next(button for button in app.button if button.label == "Prepare JSON export").click().run()

        self.assertIn(
            "Download journal export", [button.label for button in app.download_button]
        )
        self.assertIn('"Steady progress"', app.session_state["journal_export"])

        with patch("requests.get", side_effect=export_get):
            next(
                checkbox
                for checkbox in app.checkbox
                if checkbox.label
                == "I understand this permanently deletes my stored journal and plan."
            ).check().run()
        delete_response = Mock()
        delete_response.raise_for_status.return_value = None
        delete_response.json.return_value = {"deleted": True}
        with patch("requests.get", side_effect=export_get), patch(
            "requests.delete", return_value=delete_response
        ) as delete_request:
            next(button for button in app.button if button.label == "Delete all journal data").click().run()

        self.assertTrue(delete_request.call_args.args[0].endswith("/member/journal/data"))

    def test_private_journal_edits_and_deletes_one_entry(self):
        """Catches controls that render but do not target the selected member entry."""
        entry = {
            "id": "entry_opaque_1",
            "created_at": "2026-10-09T21:00:00.000000Z",
            "updated_at": "2026-10-09T21:00:00.000000Z",
            "category": "stress_or_anxiety",
            "action": "grounding_exercise",
            "note": "I paused.",
        }
        app, get = self._journal_app(entries=[entry])
        next(area for area in app.text_area if area.label == "Edit private note").set_value(
            "I paused and felt steadier."
        )
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = entry
        with patch("requests.get", side_effect=get), patch(
            "requests.patch", return_value=response
        ) as update_entry:
            next(button for button in app.button if button.label == "Save changes").click().run()

        self.assertTrue(update_entry.call_args.args[0].endswith("/member/journal/entries/entry_opaque_1"))
        self.assertEqual(
            update_entry.call_args.kwargs["json"]["note"],
            "I paused and felt steadier.",
        )

        with patch("requests.get", side_effect=get), patch(
            "requests.delete", return_value=response
        ) as delete_entry:
            next(button for button in app.button if button.label == "Delete entry").click().run()

        self.assertTrue(delete_entry.call_args.args[0].endswith("/member/journal/entries/entry_opaque_1"))

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

        # The private journal adds its own tables (see journal_store.py). The point of
        # this test is unchanged: check-ins and exercises must not persist anything,
        # so no table may exist beyond the original three plus the journal's own.
        self.assertEqual(
            tables,
            {"customers", "member_magic_links", "member_sessions"}
            | set(webhook_server.journal_store.JOURNAL_TABLES),
        )
        self.assertFalse(
            [name for name in tables if "checkin" in name or "exercise" in name]
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

    def test_privacy_policy_explains_private_journal_storage_and_deletion(self):
        """Catches deploying sensitive persistent storage without plain-language notice."""
        parser = TextCollector()
        parser.feed((ROOT / "privacy.html").read_text())
        page_text = " ".join(parser.parts).lower()

        for statement in (
            "private journal and coping plan",
            "encrypted at rest",
            "not sent to anthropic automatically",
            "json",
            "no later than 30 days",
            "subscription email address",
            "hosting-provider backups",
            "entry ids",
        ):
            self.assertIn(statement, page_text)

    def test_terms_define_the_optional_journal_and_member_controls(self):
        """Catches terms that omit the new persistent feature or misstate its limits."""
        parser = TextCollector()
        parser.feed((ROOT / "terms.html").read_text())
        page_text = " ".join(parser.parts).lower()

        for statement in (
            "private journal is optional",
            "website addresses, domains, browsing history, or dns queries",
            "export",
            "delete",
            "not sent to the ai companion automatically",
        ):
            self.assertIn(statement, page_text)

    def test_backend_exposes_no_messaging_routes(self):
        """Catches leaving a callable messaging endpoint after removing Twilio."""
        paths = app_route_paths(webhook_server.app)

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
