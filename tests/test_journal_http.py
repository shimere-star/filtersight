"""HTTP- and webhook-level tests for the private journal backend.

These exercise the real FastAPI app (``webhook_server``) through
``fastapi.testclient.TestClient``: authentication, routing, response shaping,
the Stripe webhook retention hooks, the startup cleanup, and regression checks
for checkout, sign-in, cancellation, provisioning and the one-plan entitlement.

Requirements: ``pip install -r requirements-dev.txt`` (adds httpx for TestClient).
The framework-free rules are covered in ``test_journal_store.py``.
"""

import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import secrets
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import journal_store  # noqa: E402
import webhook_server  # noqa: E402

KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()  # test-only key
OTHER_KEY = base64.urlsafe_b64encode(bytes(range(32, 64))).decode()  # test-only key
SECRET = "ZZHTTPSECRETZZ"

JOURNAL_ROUTES = [
    ("GET", "/member/journal/profile"),
    ("PATCH", "/member/journal/profile"),
    ("GET", "/member/journal/entries"),
    ("POST", "/member/journal/entries"),
    ("PATCH", "/member/journal/entries/abc123"),
    ("DELETE", "/member/journal/entries/abc123"),
    ("DELETE", "/member/journal/data"),
    ("GET", "/member/journal/export"),
]


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


def webhook_request(payload=b"{}"):
    request = Mock()
    request.body = AsyncMock(return_value=payload)
    request.headers = {"stripe-signature": "sig"}
    return request


class JournalHttpTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "customers.db")
        for patcher in (
            patch.object(webhook_server, "DB_PATH", self.db_path),
            patch.dict(os.environ, {journal_store.KEY_ENV: KEY}),
            patch.object(
                webhook_server.stripe.Subscription,
                "retrieve",
                return_value=types.SimpleNamespace(status="active", cancel_at_period_end=False),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(webhook_server.app)
        self.alice = self.add_member("alice@example.com", "sub_a", "cus_a")
        self.bob = self.add_member("bob@example.com", "sub_b", "cus_b")

    # -- helpers -----------------------------------------------------------
    def add_session(self, email, subscription):
        token = secrets.token_urlsafe(24)
        db = webhook_server.get_db()
        db.execute(
            "INSERT INTO member_sessions (token_hash, email, stripe_subscription_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                webhook_server.hash_token(token),
                email,
                subscription,
                "2099-01-01T00:00:00+00:00",
                "2026-10-10T00:00:00+00:00",
            ),
        )
        db.commit()
        db.close()
        return {"Authorization": f"Bearer {token}"}

    def add_member(self, email, subscription, customer, plan="filtersight"):
        db = webhook_server.get_db()
        db.execute(
            "INSERT INTO customers (email, stripe_customer_id, stripe_subscription_id, active, plan) VALUES (?, ?, ?, 1, ?)",
            (email, customer, subscription, plan),
        )
        db.commit()
        db.close()
        return self.add_session(email, subscription)

    def sql(self, query, params=()):
        db = webhook_server.get_db()
        try:
            return db.execute(query, params).fetchall()
        finally:
            db.close()

    def post_entry(self, headers, **fields):
        return self.client.post("/member/journal/entries", json=fields, headers=headers)


# ===========================================================================
# Authentication and routing
# ===========================================================================
class AuthenticationTests(JournalHttpTestCase):
    def test_every_journal_route_requires_a_member_session(self):
        for method, path in JOURNAL_ROUTES:
            for headers in ({}, {"Authorization": "Bearer not-a-real-token"}, {"Authorization": "Basic abc"}):
                with self.subTest(method=method, path=path, headers=bool(headers)):
                    response = self.client.request(method, path, json={"note": "x"}, headers=headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.json(), {"detail": "Sign in to continue"})

    def test_signed_out_requests_cannot_probe_the_configuration(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(journal_store.KEY_ENV, None)
            response = self.client.get("/member/journal/profile")
        self.assertEqual(response.status_code, 401)

    def test_a_retired_plan_has_no_journal_access(self):
        headers = self.add_member("old@example.com", "sub_old", "cus_old", plan="tier1")
        for method, path in JOURNAL_ROUTES:
            response = self.client.request(method, path, json={"note": "x"}, headers=headers)
            self.assertEqual(response.status_code, 403, (method, path))

    def test_an_inactive_subscription_has_no_journal_access(self):
        db = webhook_server.get_db()
        db.execute("UPDATE customers SET active = 0 WHERE email = 'bob@example.com'")
        db.commit()
        db.close()
        self.assertEqual(self.client.get("/member/journal/profile", headers=self.bob).status_code, 401)

    def test_stripe_reporting_a_cancelled_subscription_blocks_access(self):
        with patch.object(
            webhook_server.stripe.Subscription, "retrieve", return_value=types.SimpleNamespace(status="canceled")
        ):
            self.assertEqual(self.client.get("/member/journal/profile", headers=self.alice).status_code, 401)

    def test_all_journal_routes_are_registered_beside_the_existing_ones(self):
        paths = app_route_paths(webhook_server.app)
        for _, path in JOURNAL_ROUTES:
            self.assertIn(path.replace("abc123", "{entry_id}"), paths)
        for existing in (
            "/stripe-webhook",
            "/provision-nextdns-profile",
            "/member/request-link",
            "/member/verify-link",
            "/member/profile",
            "/member/chat",
            "/member/cancel",
            "/member/logout",
            "/chat",
            "/request-cancellation",
        ):
            self.assertIn(existing, paths)


# ===========================================================================
# Behaviour through HTTP
# ===========================================================================
class JournalApiTests(JournalHttpTestCase):
    def test_full_lifecycle(self):
        response = self.client.patch(
            "/member/journal/profile",
            json={"goal": "Stay focused", "why_it_matters": "My family", "coping_actions": ["walk", "call mom"]},
            headers=self.alice,
        )
        self.assertEqual(response.status_code, 200)
        profile = self.client.get("/member/journal/profile", headers=self.alice).json()
        self.assertEqual(profile["goal"], "Stay focused")
        self.assertEqual(profile["coping_actions"], ["walk", "call mom"])

        created = self.post_entry(self.alice, category="craving_or_temptation", action="breathing_exercise", note="Rough night")
        self.assertEqual(created.status_code, 201)
        entry = created.json()
        listed = self.client.get("/member/journal/entries", headers=self.alice).json()
        self.assertEqual([e["id"] for e in listed["entries"]], [entry["id"]])
        self.assertEqual(listed["limit"], 20)
        self.assertIsNone(listed["next_cursor"])

        updated = self.client.patch(
            f"/member/journal/entries/{entry['id']}", json={"note": "Better now"}, headers=self.alice
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["note"], "Better now")
        self.assertEqual(updated.json()["category"], "craving_or_temptation")

        export = self.client.get("/member/journal/export", headers=self.alice)
        self.assertEqual(export.status_code, 200)
        self.assertIn("attachment", export.headers["content-disposition"])
        self.assertIn("application/json", export.headers["content-type"])
        self.assertEqual(export.json()["profile"]["goal"], "Stay focused")
        self.assertEqual(export.json()["entries"][0]["note"], "Better now")

        deleted = self.client.delete(f"/member/journal/entries/{entry['id']}", headers=self.alice)
        self.assertEqual(deleted.json(), {"status": "deleted"})
        self.assertEqual(
            self.client.delete(f"/member/journal/entries/{entry['id']}", headers=self.alice).status_code, 404
        )

        self.post_entry(self.alice, note="again")
        wiped = self.client.delete("/member/journal/data", headers=self.alice)
        self.assertEqual(wiped.status_code, 200)
        self.assertEqual(wiped.json()["entries_deleted"], 1)
        self.assertTrue(wiped.json()["profile_deleted"])
        self.assertEqual(self.client.get("/member/journal/entries", headers=self.alice).json()["entries"], [])

    def test_members_are_isolated_over_http(self):
        entry = self.post_entry(self.alice, note="alice private note", category="loneliness").json()
        self.client.patch("/member/journal/profile", json={"goal": "alice goal"}, headers=self.alice)

        self.assertEqual(self.client.get("/member/journal/entries", headers=self.bob).json()["entries"], [])
        self.assertIsNone(self.client.get("/member/journal/profile", headers=self.bob).json()["goal"])
        stolen = self.client.patch(f"/member/journal/entries/{entry['id']}", json={"note": "hijack"}, headers=self.bob)
        gone = self.client.delete(f"/member/journal/entries/{entry['id']}", headers=self.bob)
        self.assertEqual((stolen.status_code, gone.status_code), (404, 404))
        export = self.client.get("/member/journal/export", headers=self.bob).text
        self.assertNotIn("alice", export)
        self.client.delete("/member/journal/data", headers=self.bob)
        alice_entries = self.client.get("/member/journal/entries", headers=self.alice).json()["entries"]
        self.assertEqual([e["note"] for e in alice_entries], ["alice private note"])

    def test_pagination_limit_is_enforced(self):
        for limit in ("51", "1000", "0", "abc"):
            response = self.client.get(f"/member/journal/entries?limit={limit}", headers=self.alice)
            self.assertEqual(response.status_code, 422, limit)
            self.assertEqual(response.json()["detail"][0]["field"], "limit")
        self.assertEqual(self.client.get("/member/journal/entries?limit=50", headers=self.alice).status_code, 200)

    def test_responses_are_not_cacheable(self):
        for path in ("/member/journal/profile", "/member/journal/entries", "/member/journal/export"):
            response = self.client.get(path, headers=self.alice)
            self.assertEqual(response.headers["cache-control"], "no-store", path)

    def test_write_rate_limit_returns_429_with_retry_after(self):
        for _ in range(20):
            self.assertEqual(self.post_entry(self.alice, note="x").status_code, 201)
        response = self.post_entry(self.alice, note="x")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["detail"]["reason"], "rate_limited")
        self.assertGreater(int(response.headers["retry-after"]), 0)
        self.assertEqual(self.post_entry(self.bob, note="x").status_code, 201)

    def test_oversized_request_bodies_are_rejected_without_being_parsed(self):
        response = self.client.post(
            "/member/journal/entries",
            content=(b'{"note": "' + SECRET.encode() + b'x' * 70000 + b'"}'),
            headers={**self.alice, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 413)
        self.assertNotIn(SECRET, response.text)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM journal_entries")[0][0], 0)


class SanitizedValidationOverHttpTests(JournalHttpTestCase):
    def test_validation_errors_never_echo_what_was_submitted(self):
        bodies = [
            {"note": SECRET + "a" * 2100},
            {"category": SECRET},
            {"action": [SECRET]},
            {SECRET: "value"},
            {"note": f"visit {SECRET}.com"},
            {"note": 5, "category": SECRET},
        ]
        for body in bodies:
            with self.subTest(body=str(body)[:40]):
                response = self.client.post("/member/journal/entries", json=body, headers=self.alice)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(SECRET.lower(), response.text.lower())
                for item in response.json()["detail"]:
                    self.assertLessEqual(set(item), {"field", "reason", "max_length", "max_items", "allowed", "min", "max"})
        for body in (
            {"goal": SECRET * 100},
            {"why_it_matters": SECRET * 100},
            {"coping_actions": [SECRET * 100]},
            {"coping_actions": [SECRET] * 4},
            {SECRET: SECRET},
        ):
            response = self.client.patch("/member/journal/profile", json=body, headers=self.alice)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(SECRET.lower(), response.text.lower())

    def test_malformed_json_is_reported_generically(self):
        for raw in (b'{"note": "' + SECRET.encode(), b"[" + SECRET.encode() + b"]", b"\xff\xfe" + SECRET.encode()):
            response = self.client.post(
                "/member/journal/entries",
                content=raw,
                headers={**self.alice, "Content-Type": "application/json"},
            )
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(SECRET, response.text)
            self.assertEqual(response.json()["detail"][0]["field"], "body")

    def test_the_documented_error_shape(self):
        response = self.post_entry(self.alice, note="n" * 2001)
        self.assertEqual(response.json(), {"detail": [{"field": "note", "reason": "too_long", "max_length": 2000}]})
        response = self.post_entry(self.alice, category="bored")
        self.assertEqual(
            response.json(),
            {"detail": [{"field": "category", "reason": "not_allowed", "allowed": list(journal_store.ALLOWED_CATEGORIES)}]},
        )

    def test_internal_failures_return_a_generic_body(self):
        with patch.object(journal_store, "create_entry", side_effect=RuntimeError(SECRET)):
            response = self.post_entry(self.alice, note="hello")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Something went wrong. Please try again."})
        self.assertNotIn(SECRET, response.text)

    def test_other_endpoints_keep_their_normal_validation_behaviour(self):
        # Only the journal's own bodies are sanitized; FastAPI's default handling is untouched elsewhere.
        response = self.client.post("/provision-nextdns-profile", json={})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"][0]["loc"], ["body", "checkout_session_id"])


class FailClosedOverHttpTests(JournalHttpTestCase):
    def test_missing_key_returns_a_generic_503_but_the_rest_of_the_app_works(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(journal_store.KEY_ENV, None)
            for method, path in JOURNAL_ROUTES:
                response = self.client.request(method, path, json={"note": "x"}, headers=self.alice)
                self.assertEqual(response.status_code, 503, (method, path))
                self.assertEqual(response.json(), {"detail": "The journal is not available right now."})
            profile = self.client.get("/member/profile", headers=self.alice)
            self.assertEqual(profile.status_code, 200)
            self.assertTrue(profile.json()["has_chat"])
            self.assertEqual(profile.json()["plan"], "filtersight")
            self.assertEqual(self.client.post("/member/logout", headers=self.alice).status_code, 200)

    def test_invalid_key_is_never_echoed(self):
        with patch.dict(os.environ, {journal_store.KEY_ENV: "not-a-valid-key-value"}):
            response = self.client.get("/member/journal/profile", headers=self.alice)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("not-a-valid-key-value", response.text)

    def test_wrong_key_is_refused_and_the_right_key_still_reads_the_data(self):
        self.post_entry(self.alice, note="kept safe")
        with patch.dict(os.environ, {journal_store.KEY_ENV: OTHER_KEY}):
            for method, path in JOURNAL_ROUTES:
                response = self.client.request(method, path, json={"note": "x"}, headers=self.alice)
                self.assertEqual(response.status_code, 503, (method, path))
                self.assertNotIn(OTHER_KEY, response.text)
        entries = self.client.get("/member/journal/entries", headers=self.alice).json()["entries"]
        self.assertEqual([e["note"] for e in entries], ["kept safe"])


class EncryptionAtRestOverHttpTests(JournalHttpTestCase):
    def test_nothing_sensitive_reaches_the_database_file(self):
        sentinels = ["HTTPSENTINELGOAL", "HTTPSENTINELWHY", "HTTPSENTINELCOPEA", "HTTPSENTINELNOTE"]
        self.client.patch(
            "/member/journal/profile",
            json={"goal": sentinels[0], "why_it_matters": sentinels[1], "coping_actions": [sentinels[2]]},
            headers=self.alice,
        )
        self.post_entry(self.alice, category="boredom", note=sentinels[3])
        raw = Path(self.db_path).read_bytes()
        for sentinel in sentinels:
            self.assertNotIn(sentinel.encode(), raw)


class LoggingOverHttpTests(JournalHttpTestCase):
    def test_journal_content_never_reaches_the_logs(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        out, err = io.StringIO(), io.StringIO()
        sentinels = ["HTTPLOGGOAL", "HTTPLOGNOTE", "HTTPLOGBAD"]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.client.patch("/member/journal/profile", json={"goal": sentinels[0]}, headers=self.alice)
                entry = self.post_entry(self.alice, note=sentinels[1]).json()
                self.client.patch(f"/member/journal/entries/{entry['id']}", json={"note": sentinels[1] + "2"}, headers=self.alice)
                self.post_entry(self.alice, note=sentinels[2] + "x" * 3000)
                self.post_entry(self.alice, category=sentinels[2])
                self.client.post(
                    "/member/journal/entries",
                    content=('{"note": "' + sentinels[2]).encode(),
                    headers={**self.alice, "Content-Type": "application/json"},
                )
                with patch.object(journal_store, "create_entry", side_effect=RuntimeError(sentinels[2])):
                    self.post_entry(self.alice, note=sentinels[1])
                self.client.get("/member/journal/export", headers=self.alice)
                self.client.delete("/member/journal/data", headers=self.alice)
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        combined = stream.getvalue() + out.getvalue() + err.getvalue()
        for sentinel in sentinels + [KEY, OTHER_KEY]:
            self.assertNotIn(sentinel, combined)


# ===========================================================================
# Subscription events (Stripe webhook) and retention
# ===========================================================================
class RetentionWebhookTests(JournalHttpTestCase):
    NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)

    def setUp(self):
        super().setUp()
        patcher = patch.object(journal_store, "utcnow", lambda: self.NOW)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.post_entry(self.alice, note="alice note")
        self.client.patch("/member/journal/profile", json={"goal": "alice goal"}, headers=self.alice)
        self.post_entry(self.bob, note="bob note")

    def send_event(self, event):
        with patch.object(webhook_server.stripe.Webhook, "construct_event", return_value=event):
            return asyncio.run(webhook_server.stripe_webhook(webhook_request()))

    def subscription_deleted(self, customer="cus_a", subscription="sub_a", ended_at=None):
        ended_at = ended_at or (self.NOW - timedelta(hours=1))
        return {
            "type": "customer.subscription.deleted",
            "data": {
                "object": types.SimpleNamespace(
                    id=subscription, customer=customer, ended_at=int(ended_at.timestamp())
                )
            },
        }

    def checkout_completed(self, email="alice@example.com", customer="cus_a", subscription="sub_a2", plan="filtersight"):
        return {
            "type": "checkout.session.completed",
            "data": {
                "object": types.SimpleNamespace(
                    customer_details=types.SimpleNamespace(email=email),
                    customer=customer,
                    subscription=subscription,
                    metadata=types.SimpleNamespace(to_dict=lambda: {"plan": plan}),
                )
            },
        }

    def retention(self, email="alice@example.com"):
        rows = self.sql(
            "SELECT subscription_ended_at, delete_after, purged_at FROM journal_retention WHERE member_email = ?",
            (email,),
        )
        return rows[0] if rows else None

    def active(self, email):
        return self.sql("SELECT active FROM customers WHERE email = ?", (email,))[0][0]

    def test_subscription_end_deactivates_as_before_and_schedules_deletion(self):
        self.assertEqual(self.send_event(self.subscription_deleted()), {"status": "ok"})
        self.assertEqual(self.active("alice@example.com"), 0)  # existing behaviour unchanged
        self.assertEqual(self.active("bob@example.com"), 1)
        ended_at, delete_after, purged_at = self.retention()
        self.assertEqual(delete_after, journal_store.to_ts(self.NOW - timedelta(hours=1) + timedelta(days=29)))
        self.assertIsNone(purged_at)
        self.assertIsNone(self.retention("bob@example.com"))
        # Signed-out members cannot reach their journal while it awaits deletion.
        self.assertEqual(self.client.get("/member/journal/profile", headers=self.alice).status_code, 401)

    def test_a_deletion_event_for_an_older_subscription_is_ignored_for_retention(self):
        self.send_event(self.subscription_deleted(subscription="sub_some_old_one"))
        self.assertIsNone(self.retention())

    def test_returning_before_the_deadline_keeps_the_journal(self):
        self.send_event(self.subscription_deleted())
        self.send_event(self.checkout_completed())
        self.assertEqual(self.client.get("/member/journal/entries", headers=self.alice).status_code, 401)
        reactivated = self.add_session("alice@example.com", "sub_a2")
        self.assertEqual(self.active("alice@example.com"), 1)
        self.assertIsNone(self.retention())
        db_notes = self.client.get("/member/journal/entries", headers=reactivated).json()["entries"]
        self.assertEqual([e["note"] for e in db_notes], ["alice note"])

    def test_returning_after_the_deadline_does_not_resurrect_deleted_data(self):
        self.send_event(self.subscription_deleted())
        later = self.NOW + timedelta(days=35)
        db = webhook_server.get_db()
        journal_store.run_cleanup(webhook_server.get_db, now=later)
        db.close()
        purged_at = self.retention()[2]
        self.assertIsNotNone(purged_at)
        self.send_event(self.checkout_completed())
        self.assertEqual(self.client.get("/member/journal/entries", headers=self.alice).status_code, 401)
        reactivated = self.add_session("alice@example.com", "sub_a2")
        self.assertEqual(self.retention()[2], purged_at)  # still on record
        self.assertEqual(self.client.get("/member/journal/entries", headers=reactivated).json()["entries"], [])
        self.assertIsNone(self.client.get("/member/journal/profile", headers=reactivated).json()["goal"])

    def test_a_deletion_that_is_already_due_is_not_rescued_by_returning(self):
        self.send_event(self.subscription_deleted())
        with patch.object(journal_store, "utcnow", lambda: self.NOW + timedelta(days=30)):
            self.send_event(self.checkout_completed())
        self.assertEqual(self.sql("SELECT COUNT(*) FROM journal_entries WHERE member_email = 'alice@example.com'")[0][0], 0)
        self.assertIsNotNone(self.retention()[2])

    def test_a_retired_plan_checkout_is_still_ignored(self):
        self.send_event(self.checkout_completed(email="new@example.com", customer="cus_n", subscription="sub_n", plan="tier1"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM customers WHERE email = 'new@example.com'")[0][0], 0)

    def test_a_current_plan_checkout_creates_the_customer_as_before(self):
        self.send_event(self.checkout_completed(email="new@example.com", customer="cus_n", subscription="sub_n"))
        row = self.sql("SELECT stripe_customer_id, stripe_subscription_id, active, plan FROM customers WHERE email = 'new@example.com'")[0]
        self.assertEqual(row, ("cus_n", "sub_n", 1, "filtersight"))

    def test_a_journal_bookkeeping_failure_cannot_break_billing(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        with patch.object(journal_store, "schedule_deletion_for_customer", side_effect=RuntimeError(SECRET)):
            self.assertEqual(self.send_event(self.subscription_deleted()), {"status": "ok"})
        self.assertEqual(self.active("alice@example.com"), 0)
        self.assertIn("RuntimeError", stream.getvalue())
        self.assertNotIn(SECRET, stream.getvalue())
        with patch.object(journal_store, "clear_pending_deletion", side_effect=RuntimeError(SECRET)):
            self.assertEqual(self.send_event(self.checkout_completed()), {"status": "ok"})
        self.assertEqual(self.active("alice@example.com"), 1)

    def test_provisioning_after_checkout_also_keeps_a_pending_journal(self):
        self.send_event(self.subscription_deleted())
        checkout = {"email": "alice@example.com", "plan": "filtersight", "customer_id": "cus_a", "subscription_id": "sub_a2"}
        with (
            patch.object(webhook_server, "verify_paid_checkout", return_value=checkout),
            patch.object(webhook_server, "create_nextdns_profile", return_value="profile_x"),
        ):
            response = self.client.post("/provision-nextdns-profile", json={"checkout_session_id": "cs_1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"profile_id": "profile_x"})
        self.assertEqual(self.active("alice@example.com"), 1)
        self.assertIsNone(self.retention())

    def test_cancelling_schedules_nothing_and_leaves_the_journal_alone(self):
        with patch.object(webhook_server.stripe.Subscription, "modify") as modify:
            response = self.client.post("/member/cancel", headers=self.alice)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cancellation_fee"], 0)
        modify.assert_called_once_with("sub_a", cancel_at_period_end=True)
        self.assertIsNone(self.retention())
        self.assertEqual(self.client.get("/member/journal/entries", headers=self.alice).json()["entries"][0]["note"], "alice note")


class StartupCleanupTests(JournalHttpTestCase):
    def test_startup_sweep_deletes_what_came_due_while_the_service_was_down(self):
        self.post_entry(self.alice, note="overdue")
        self.post_entry(self.bob, note="still subscribed")
        db = webhook_server.get_db()
        db.execute("UPDATE customers SET active = 0 WHERE email = 'alice@example.com'")
        journal_store.schedule_deletion(db, "alice@example.com", datetime(2020, 1, 1, tzinfo=timezone.utc))
        db.commit()
        db.close()
        with TestClient(webhook_server.app):  # entering the context runs the app's startup
            pass
        self.assertEqual(self.sql("SELECT COUNT(*) FROM journal_entries WHERE member_email = 'alice@example.com'")[0][0], 0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM journal_entries WHERE member_email = 'bob@example.com'")[0][0], 1)

    def test_app_starts_even_if_the_database_is_unreachable(self):
        with patch.object(webhook_server, "DB_PATH", "/nonexistent-directory/customers.db"):
            with TestClient(webhook_server.app) as client:
                self.assertEqual(client.get("/member/journal/profile").status_code, 401)


# ===========================================================================
# Regression: sign-in, checkout verification, entitlement, migration
# ===========================================================================
class RegressionTests(JournalHttpTestCase):
    def test_sign_in_link_flow_still_works_and_unlocks_the_journal(self):
        sent = {}

        def capture(email, token):
            sent["email"], sent["token"] = email, token

        with (
            patch.object(webhook_server, "SENDGRID_API_KEY", "key"),
            patch.object(webhook_server, "SENDGRID_FROM_EMAIL", "from@example.com"),
            patch.object(webhook_server, "send_member_signin_email", side_effect=capture),
        ):
            requested = self.client.post("/member/request-link", json={"email": "ALICE@example.com"})
        self.assertEqual(requested.status_code, 200)
        self.assertEqual(sent["email"], "alice@example.com")
        verified = self.client.post("/member/verify-link", json={"token": sent["token"]})
        self.assertEqual(verified.status_code, 200)
        headers = {"Authorization": f"Bearer {verified.json()['access_token']}"}
        self.assertEqual(self.client.get("/member/journal/profile", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/member/logout", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/member/journal/profile", headers=headers).status_code, 401)

    def test_retired_tier_checkout_is_still_rejected_over_http(self):
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
        with (
            patch.object(webhook_server.stripe.checkout.Session, "retrieve", return_value=session),
            patch.object(webhook_server.stripe, "api_key", "sk_test_123"),
        ):
            response = self.client.post("/provision-nextdns-profile", json={"checkout_session_id": "cs_1"})
        self.assertEqual(response.status_code, 403)

    def test_the_single_plan_entitlement_gives_chat_and_journal_to_every_subscriber(self):
        profile = self.client.get("/member/profile", headers=self.alice).json()
        self.assertEqual((profile["plan"], profile["has_chat"]), ("filtersight", True))
        self.assertEqual(self.client.get("/member/journal/profile", headers=self.alice).status_code, 200)

    def test_the_messaging_routes_stay_removed(self):
        paths = app_route_paths(webhook_server.app)
        for removed in ("/sms-webhook", "/save-contact", "/notify-attempt", "/poll-nextdns-and-notify", "/check-for-removed-profiles"):
            self.assertNotIn(removed, paths)


class MigrationThroughGetDbTests(unittest.TestCase):
    def test_real_get_db_migrates_a_production_shaped_database_additively(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "customers.db")
            conn = sqlite3.connect(path)
            conn.execute(
                """CREATE TABLE customers (
                       email TEXT PRIMARY KEY, stripe_customer_id TEXT, stripe_subscription_id TEXT,
                       active INTEGER DEFAULT 1, plan TEXT, nextdns_profile_id TEXT,
                       removal_fee_paid INTEGER DEFAULT 0)"""
            )
            for column in ("user_phone TEXT", "accountability_phone TEXT", "partner_opt_in_status TEXT"):
                conn.execute(f"ALTER TABLE customers ADD COLUMN {column}")
            conn.execute(
                "INSERT INTO customers (email, stripe_customer_id, stripe_subscription_id, active, plan, nextdns_profile_id, user_phone) VALUES ('m@example.com', 'cus_1', 'sub_1', 1, 'filtersight', 'abc123', '+15551234567')"
            )
            conn.execute(
                "CREATE TABLE member_magic_links (email TEXT PRIMARY KEY, token_hash TEXT NOT NULL, expires_at TEXT NOT NULL, sent_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE member_sessions (token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, stripe_subscription_id TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            conn.commit()
            legacy_schema = {
                name: conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]
                for name in ("customers", "member_magic_links", "member_sessions")
            }
            conn.close()

            with patch.object(webhook_server, "DB_PATH", path):
                for _ in range(2):  # get_db runs on every request: must be idempotent
                    migrated = webhook_server.get_db()
                    migrated.close()
                migrated = webhook_server.get_db()
                tables = {r[0] for r in migrated.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                row = migrated.execute(
                    "SELECT email, stripe_customer_id, stripe_subscription_id, active, plan, nextdns_profile_id, user_phone FROM customers"
                ).fetchall()
                schema_after = {
                    name: migrated.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]
                    for name in legacy_schema
                }
                migrated.close()

        self.assertEqual(tables, {"customers", "member_magic_links", "member_sessions"} | set(journal_store.JOURNAL_TABLES))
        # Existing rows survive; the pre-existing legacy-messaging purge still runs.
        self.assertEqual(row, [("m@example.com", "cus_1", "sub_1", 1, "filtersight", "abc123", None)])
        # No existing table definition was changed (the legacy columns were already there).
        self.assertEqual(schema_after, legacy_schema)


if __name__ == "__main__":
    unittest.main()
