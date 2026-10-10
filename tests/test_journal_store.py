"""Tests for the private journal storage, validation, limits and retention.

These exercise ``journal_store`` and ``journal_service`` directly, so they need
only the standard library and ``cryptography`` (no web framework). The HTTP and
webhook wiring is covered in ``test_journal_http.py``.
"""

import ast
import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import journal_service  # noqa: E402
import journal_store as store  # noqa: E402

KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()  # test-only key
OTHER_KEY = base64.urlsafe_b64encode(bytes(range(32, 64))).decode()  # test-only key

ALICE = "alice@example.com"
BOB = "bob@example.com"

LEGACY_COLUMNS = (
    "user_phone TEXT",
    "accountability_phone TEXT",
    "user_sms_opted_in INTEGER DEFAULT 0",
    "user_sms_consent_at TEXT",
    "user_sms_consent_version TEXT",
    "accountability_sms_opted_in INTEGER DEFAULT 0",
    "partner_opt_in_status TEXT",
    "partner_opt_in_confirmed_at TEXT",
)


def create_production_shaped_database(path):
    """The three tables FilterSight's get_db() creates today, including the
    legacy messaging columns older production databases still carry."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE customers (
               email TEXT PRIMARY KEY,
               stripe_customer_id TEXT,
               stripe_subscription_id TEXT,
               active INTEGER DEFAULT 1,
               plan TEXT,
               nextdns_profile_id TEXT,
               removal_fee_paid INTEGER DEFAULT 0
           )"""
    )
    for column in LEGACY_COLUMNS:
        conn.execute(f"ALTER TABLE customers ADD COLUMN {column}")
    conn.execute(
        """CREATE TABLE member_magic_links (
               email TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
               expires_at TEXT NOT NULL, sent_at TEXT NOT NULL)"""
    )
    conn.execute(
        """CREATE TABLE member_sessions (
               token_hash TEXT PRIMARY KEY, email TEXT NOT NULL,
               stripe_subscription_id TEXT NOT NULL,
               expires_at TEXT NOT NULL, created_at TEXT NOT NULL)"""
    )
    conn.commit()
    conn.close()


class JournalTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "customers.db")
        create_production_shaped_database(self.db_path)
        self.now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        self.environ = {store.KEY_ENV: KEY}
        self.service = journal_service.JournalService(
            self.get_db, environ=self.environ, clock=lambda: self.now
        )

    # -- helpers -----------------------------------------------------------
    def get_db(self):
        conn = sqlite3.connect(self.db_path)
        store.ensure_schema(conn)
        conn.commit()
        # Simulate a SQLite build that does NOT overwrite deleted data by default,
        # so the tests prove our code turns secure_delete on rather than relying
        # on how the local SQLite happens to be compiled.
        conn.execute("PRAGMA secure_delete = OFF")
        return conn

    def add_member(self, email, *, subscription="sub_1", customer="cus_1", active=1):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO customers (email, stripe_customer_id, stripe_subscription_id, active, plan) VALUES (?, ?, ?, ?, 'filtersight')",
            (email, customer, subscription, active),
        )
        conn.commit()
        conn.close()

    def tick(self, **delta):
        self.now += timedelta(**delta)

    def call(self, method, *args):
        return getattr(self.service, method)(*args)

    def create(self, member=ALICE, **fields):
        return self.service.entry_create(member, json.dumps(fields).encode())

    def patch_profile(self, member=ALICE, **fields):
        return self.service.profile_update(member, json.dumps(fields).encode())

    def sql(self, query, params=()):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    def count(self, table, member=None):
        if member is None:
            return self.sql(f"SELECT COUNT(*) FROM {table}")[0][0]
        return self.sql(f"SELECT COUNT(*) FROM {table} WHERE member_email = ?", (member,))[0][0]

    def raw_db_bytes(self):
        return Path(self.db_path).read_bytes()

    def dump_all_tables(self):
        conn = sqlite3.connect(self.db_path)
        try:
            names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return "\n".join(repr(conn.execute(f"SELECT * FROM {n}").fetchall()) for n in names)
        finally:
            conn.close()

    def seed_entries(self, member, total, *, start=None):
        """Insert entries directly (bypassing rate limits) for list/cap tests."""
        cipher = store.load_cipher(self.environ)
        conn = self.get_db()
        start = start or (self.now - timedelta(days=2))
        ids = []
        for index in range(total):
            entry_id = f"seed{index:05d}"
            stamp = store.to_ts(start + timedelta(seconds=index))
            conn.execute(
                "INSERT INTO journal_entries (entry_id, member_email, created_at, updated_at, category, action, note_enc) VALUES (?, ?, ?, ?, 'boredom', NULL, ?)",
                (entry_id, member, stamp, stamp, cipher.encrypt(f"note {index}", field="entry.note", row_id=entry_id)),
            )
            ids.append(entry_id)
        conn.commit()
        conn.close()
        return ids


# ===========================================================================
# Encryption key handling
# ===========================================================================
class KeyHandlingTests(unittest.TestCase):
    def test_valid_key_loads(self):
        self.assertIsInstance(store.load_cipher({store.KEY_ENV: KEY}), store.FieldCipher)

    def test_missing_or_blank_key_fails_closed(self):
        for environ in ({}, {store.KEY_ENV: ""}, {store.KEY_ENV: "   "}):
            with self.assertRaises(store.JournalUnavailable):
                store.load_cipher(environ)

    def test_invalid_keys_are_rejected_without_echoing_them(self):
        bad_keys = [
            "not-base64!!",
            "short",
            base64.urlsafe_b64encode(b"x" * 16).decode(),  # 16 bytes
            base64.urlsafe_b64encode(b"x" * 33).decode(),  # 33 bytes
            base64.urlsafe_b64encode(b"x" * 31).decode(),
            KEY.rstrip("="),  # not canonical (padding removed)
            KEY.replace("-", "+").replace("_", "/") + "\n" + KEY,
            "é" * 44,
        ]
        # A standard-alphabet encoding of the same bytes is not the canonical form.
        standard = base64.b64encode(bytes([251] * 32)).decode()
        self.assertTrue("+" in standard or "/" in standard)
        bad_keys.append(standard)
        for bad in bad_keys:
            with self.subTest(key=bad[:6]):
                with self.assertRaises(store.JournalUnavailable) as raised:
                    store.load_cipher({store.KEY_ENV: bad})
                self.assertNotIn(bad, str(raised.exception))

    def test_key_with_surrounding_whitespace_is_accepted(self):
        self.assertIsInstance(store.load_cipher({store.KEY_ENV: f"  {KEY}\n"}), store.FieldCipher)


class CipherTests(unittest.TestCase):
    def setUp(self):
        self.cipher = store.load_cipher({store.KEY_ENV: KEY})

    def test_round_trip_and_unicode(self):
        for text in ("hello", "naïve — 日本語 😀", "multi\nline"):
            token = self.cipher.encrypt(text, field="entry.note", row_id="abc")
            self.assertTrue(token.startswith("v1:"))
            self.assertNotIn(text, token)
            self.assertEqual(self.cipher.decrypt(token, field="entry.note", row_id="abc"), text)

    def test_same_plaintext_encrypts_differently_each_time(self):
        a = self.cipher.encrypt("same", field="f", row_id="r")
        b = self.cipher.encrypt("same", field="f", row_id="r")
        self.assertNotEqual(a, b)

    def test_tampering_is_detected(self):
        token = self.cipher.encrypt("secret", field="f", row_id="r")
        prefix, body = token.split(":", 1)
        raw = bytearray(base64.urlsafe_b64decode(body))
        raw[-1] ^= 1
        forged = prefix + ":" + base64.urlsafe_b64encode(bytes(raw)).decode()
        with self.assertRaises(store.JournalUnavailable):
            self.cipher.decrypt(forged, field="f", row_id="r")

    def test_ciphertext_is_bound_to_its_field_and_row(self):
        token = self.cipher.encrypt("secret", field="entry.note", row_id="row1")
        for field, row in (("entry.note", "row2"), ("profile.goal", "row1")):
            with self.assertRaises(store.JournalUnavailable):
                self.cipher.decrypt(token, field=field, row_id=row)

    def test_wrong_key_and_garbage_fail(self):
        token = self.cipher.encrypt("secret", field="f", row_id="r")
        other = store.load_cipher({store.KEY_ENV: OTHER_KEY})
        with self.assertRaises(store.JournalUnavailable):
            other.decrypt(token, field="f", row_id="r")
        for garbage in ("", "v1:", "v2:AAAA", "v1:!!!!", "nonsense", "v1:AAAA"):
            with self.assertRaises(store.JournalUnavailable):
                self.cipher.decrypt(garbage, field="f", row_id="r")


# ===========================================================================
# Text normalization and validation
# ===========================================================================
class NormalizationTests(unittest.TestCase):
    def norm(self, value, **kwargs):
        kwargs.setdefault("field", "note")
        kwargs.setdefault("max_length", 2000)
        return store.normalize_text(value, **kwargs)

    def test_whitespace_and_newlines(self):
        self.assertEqual(self.norm("  hello   world  "), "hello world")
        self.assertEqual(self.norm("a\nb\tc"), "a b c")
        self.assertEqual(self.norm("a\r\nb\rc", multiline=True), "a\nb\nc")
        self.assertEqual(self.norm("a\n\n\n\nb  \n  c", multiline=True), "a\n\nb\nc")

    def test_control_and_bidi_characters_are_stripped(self):
        self.assertEqual(self.norm("a\x00b\x07c"), "abc")
        self.assertEqual(self.norm("abc‮def⁦x"), "abcdefx")
        self.assertEqual(self.norm("a​b﻿c"), "abc")
        self.assertEqual(self.norm("a b c"), "a b c")

    def test_unicode_is_normalized_to_nfc(self):
        self.assertEqual(self.norm("é"), "é")

    def test_empty_becomes_none(self):
        for value in (None, "", "   ", "\n\t \x00"):
            self.assertIsNone(self.norm(value))

    def test_non_text_and_unencodable_text_rejected(self):
        for value in (5, 1.5, True, ["a"], {"a": 1}):
            with self.assertRaises(store.JournalValidationError) as raised:
                self.norm(value)
            self.assertEqual(raised.exception.reason, "must_be_text")
        with self.assertRaises(store.JournalValidationError) as raised:
            self.norm("bad \ud800 surrogate")
        self.assertEqual(raised.exception.reason, "invalid_characters")

    def test_length_limits_apply_after_normalization(self):
        for limit in (store.GOAL_MAX, store.WHY_MAX, store.COPING_ITEM_MAX, store.NOTE_MAX):
            self.assertEqual(len(self.norm("a" * limit, max_length=limit)), limit)
            with self.assertRaises(store.JournalValidationError) as raised:
                self.norm("a" * (limit + 1), max_length=limit)
            self.assertEqual(raised.exception.reason, "too_long")
            self.assertEqual(raised.exception.public_details["max_length"], limit)
        self.assertEqual(len(self.norm(" " * 50 + "a" * 500 + " " * 50, max_length=500)), 500)

    def test_length_counts_characters_not_bytes(self):
        self.assertEqual(len(self.norm("😀" * 2000, max_length=2000)), 2000)
        with self.assertRaises(store.JournalValidationError):
            self.norm("😀" * 2001, max_length=2000)

    def test_web_addresses_are_rejected(self):
        for text in (
            "https://example.com/page",
            "see example.com",
            "WWW.Test.org is where it happened",
            "visit sub.domain.co",
            "ftp://files.local",
            "found it at 192.168.1.20",
            "mail me@site.com",
            "link bit.ly",
            "went to someplace.xxx last night",
        ):
            with self.subTest(text=text):
                with self.assertRaises(store.JournalValidationError) as raised:
                    self.norm(text)
                self.assertEqual(raised.exception.reason, "web_address_not_allowed")

    def test_ordinary_sentences_are_not_mistaken_for_addresses(self):
        for text in (
            "Dr. Smith helped me. Then we talked.",
            "I slept 3.5 hours, version 1.2 of my plan",
            "e.g. when I'm bored, I walk",
            "I said it. So I tried.",
            "Mr.Jones called",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(self.norm(text))


class PayloadValidationTests(unittest.TestCase):
    def test_allowlists_are_exactly_the_specified_values(self):
        self.assertEqual(
            store.ALLOWED_CATEGORIES,
            (
                "craving_or_temptation",
                "stress_or_anxiety",
                "boredom",
                "loneliness",
                "accidental_block",
                "legitimate_access_need",
                "something_else",
            ),
        )
        self.assertEqual(
            store.ALLOWED_ACTIONS,
            (
                "breathing_exercise",
                "grounding_exercise",
                "distraction",
                "five_minute_cooldown",
                "ten_minute_cooldown",
                "talked_it_through",
                "contacted_trusted_person",
                "did_something_else",
            ),
        )

    def test_limits_are_the_specified_values(self):
        self.assertEqual(
            (store.GOAL_MAX, store.WHY_MAX, store.COPING_MAX_ITEMS, store.COPING_ITEM_MAX, store.NOTE_MAX),
            (500, 1000, 3, 250, 2000),
        )
        self.assertEqual(
            (store.MAX_ENTRIES_PER_MEMBER, store.ENTRY_WRITES_PER_HOUR, store.PROFILE_WRITES_PER_HOUR, store.PAGE_MAX),
            (500, 20, 10, 50),
        )

    def test_unparseable_bodies(self):
        for raw in (b"", b"not json", b'{"note": "x', b"\xff\xfe", b'{"a": NaN}', b'{"a": 1, "a": 2}', b"[" * 10000):
            with self.assertRaises(store.JournalValidationError) as raised:
                store.parse_json_object(raw)
            self.assertEqual(raised.exception.reason, "invalid_json")
        for raw in (b"[]", b'"text"', b"5", b"null"):
            with self.assertRaises(store.JournalValidationError) as raised:
                store.parse_json_object(raw)
            self.assertEqual(raised.exception.reason, "must_be_object")

    def test_forbidden_data_has_no_field_to_live_in(self):
        for name in ("url", "domain", "website", "location", "browsing_history", "dns_query", "social_media", "ip"):
            for validate in (store.validate_entry_changes, store.validate_profile_changes):
                with self.subTest(name=name, validate=validate.__name__):
                    with self.assertRaises(store.JournalValidationError) as raised:
                        validate({"note": "ok", "goal": "ok", name: "x"})
                    self.assertEqual(raised.exception.reason, "unknown_field")

    def test_invalid_categories_and_actions(self):
        for field in ("category", "action"):
            for bad in ("Boredom", "bored", "", " boredom", 5, True, ["boredom"], {"x": 1}, "stress or anxiety"):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(store.JournalValidationError) as raised:
                        store.validate_entry_changes({field: bad})
                    error = raised.exception.as_dict()
                    self.assertEqual(error["reason"], "not_allowed")
                    self.assertEqual(error["field"], field)
                    allowed = store.ALLOWED_CATEGORIES if field == "category" else store.ALLOWED_ACTIONS
                    self.assertEqual(error["allowed"], list(allowed))

    def test_coping_actions_rules(self):
        self.assertEqual(store.normalize_coping_actions(None), [])
        self.assertEqual(store.normalize_coping_actions(["walk", "  call a friend ", "   "]), ["walk", "call a friend"])
        # The raw list is capped at three items before blanks are dropped.
        with self.assertRaises(store.JournalValidationError):
            store.normalize_coping_actions(["walk", "", "", ""])
        self.assertEqual(len(store.normalize_coping_actions(["a", "b", "c"])), 3)
        with self.assertRaises(store.JournalValidationError) as raised:
            store.normalize_coping_actions(["a", "b", "c", "d"])
        self.assertEqual(raised.exception.as_dict(), {"field": "coping_actions", "reason": "too_many_items", "max_items": 3})
        with self.assertRaises(store.JournalValidationError) as raised:
            store.normalize_coping_actions("walk")
        self.assertEqual(raised.exception.reason, "must_be_list")
        with self.assertRaises(store.JournalValidationError) as raised:
            store.normalize_coping_actions([5])
        self.assertEqual(raised.exception.reason, "must_be_text")
        self.assertEqual(len(store.normalize_coping_actions(["x" * 250])[0]), 250)
        with self.assertRaises(store.JournalValidationError) as raised:
            store.normalize_coping_actions(["x" * 251])
        self.assertEqual(raised.exception.public_details["max_length"], 250)

    def test_empty_patches_are_rejected(self):
        for validate in (store.validate_entry_changes, store.validate_profile_changes):
            with self.assertRaises(store.JournalValidationError) as raised:
                validate({})
            self.assertEqual(raised.exception.reason, "no_fields_provided")

    def test_pagination_parameter_bounds(self):
        self.assertEqual(store.parse_limit(None), 20)
        self.assertEqual(store.parse_limit("1"), 1)
        self.assertEqual(store.parse_limit("50"), 50)
        for bad in ("0", "51", "1000", "-1", "abc", "", "1.5", "99999"):
            with self.assertRaises(store.JournalValidationError) as raised:
                store.parse_limit(bad)
            self.assertEqual(raised.exception.as_dict(), {"field": "limit", "reason": "out_of_range", "min": 1, "max": 50})

    def test_cursor_validation(self):
        good = store.encode_cursor("2026-10-10T12:00:00.000000Z", "abc_DEF-1")
        self.assertEqual(store.decode_cursor(good), ("2026-10-10T12:00:00.000000Z", "abc_DEF-1"))
        for bad in ("", "!!!", "x" * 201, base64.urlsafe_b64encode(b"nope").decode(), base64.urlsafe_b64encode(b"a|b|c").decode()):
            with self.assertRaises(store.JournalValidationError) as raised:
                store.decode_cursor(bad)
            self.assertEqual(raised.exception.reason, "invalid_cursor")


# ===========================================================================
# Profile and entries through the service (authenticated-member logic)
# ===========================================================================
class ProfileTests(JournalTestCase):
    def test_empty_profile(self):
        response = self.call("profile_get", ALICE)
        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.body,
            {"goal": None, "why_it_matters": None, "coping_actions": [], "updated_at": None},
        )

    def test_set_get_partial_update_and_clear(self):
        response = self.patch_profile(
            goal="Stay focused", why_it_matters="My family\n\nand my health", coping_actions=["walk", "call mom", "cold water"]
        )
        self.assertEqual(response.status, 200)
        profile = self.call("profile_get", ALICE).body
        self.assertEqual(profile["goal"], "Stay focused")
        self.assertEqual(profile["why_it_matters"], "My family\n\nand my health")
        self.assertEqual(profile["coping_actions"], ["walk", "call mom", "cold water"])

        self.tick(minutes=1)
        self.patch_profile(goal="New goal")  # omitted fields are unchanged
        profile = self.call("profile_get", ALICE).body
        self.assertEqual(profile["goal"], "New goal")
        self.assertEqual(profile["coping_actions"], ["walk", "call mom", "cold water"])

        self.tick(minutes=1)
        self.patch_profile(coping_actions=["breathe"], why_it_matters=None)
        profile = self.call("profile_get", ALICE).body
        self.assertEqual(profile["coping_actions"], ["breathe"])
        self.assertIsNone(profile["why_it_matters"])
        self.assertEqual(profile["goal"], "New goal")

    def test_clearing_everything_removes_the_profile(self):
        self.patch_profile(goal="x", coping_actions=["y"])
        self.tick(minutes=1)
        self.patch_profile(goal=None, coping_actions=[])
        self.assertEqual(self.count("journal_profiles"), 0)
        self.assertEqual(self.call("profile_get", ALICE).body["updated_at"], None)

    def test_profile_limits_through_the_api(self):
        for fields in (
            {"goal": "g" * 501},
            {"why_it_matters": "w" * 1001},
            {"coping_actions": ["c" * 251]},
            {"coping_actions": ["a", "b", "c", "d"]},
        ):
            response = self.patch_profile(**fields)
            self.assertEqual(response.status, 422, fields)
        self.assertEqual(self.patch_profile(goal="g" * 500, why_it_matters="w" * 1000, coping_actions=["c" * 250] * 3).status, 200)
        self.assertEqual(self.count("journal_profiles"), 1)


class EntryTests(JournalTestCase):
    def test_create_read_update_delete(self):
        created = self.create(category="craving_or_temptation", action="breathing_exercise", note="Tough evening")
        self.assertEqual(created.status, 201)
        entry = created.body
        self.assertEqual(set(entry), {"id", "created_at", "updated_at", "category", "action", "note"})
        self.assertEqual(entry["note"], "Tough evening")
        self.assertRegex(entry["id"], r"^[A-Za-z0-9_-]{20,}$")

        listed = self.call("entries_list", ALICE, {}).body
        self.assertEqual([e["id"] for e in listed["entries"]], [entry["id"]])
        self.assertEqual(listed["entries"][0]["note"], "Tough evening")
        self.assertIsNone(listed["next_cursor"])

        self.tick(minutes=5)
        updated = self.service.entry_update(ALICE, entry["id"], json.dumps({"note": "Better now", "action": "grounding_exercise"}).encode())
        self.assertEqual(updated.status, 200)
        self.assertEqual(updated.body["note"], "Better now")
        self.assertEqual(updated.body["category"], "craving_or_temptation")
        self.assertEqual(updated.body["created_at"], entry["created_at"])
        self.assertGreater(updated.body["updated_at"], entry["updated_at"])

        cleared = self.service.entry_update(ALICE, entry["id"], b'{"note": null}')
        self.assertIsNone(cleared.body["note"])
        self.assertEqual(cleared.body["action"], "grounding_exercise")

        self.assertEqual(self.call("entry_delete", ALICE, entry["id"]).status, 200)
        self.assertEqual(self.call("entries_list", ALICE, {}).body["entries"], [])
        self.assertEqual(self.call("entry_delete", ALICE, entry["id"]).status, 404)

    def test_entry_needs_some_content_and_cannot_be_emptied(self):
        for fields in ({"note": "   "}, {"category": None}, {"note": None, "action": None}):
            self.assertEqual(self.create(**fields).status, 422, fields)
        self.assertEqual(self.service.entry_create(ALICE, b"{}").status, 422)
        entry = self.create(note="only note").body
        response = self.service.entry_update(ALICE, entry["id"], b'{"note": null}')
        self.assertEqual(response.status, 422)
        self.assertEqual(self.call("entries_list", ALICE, {}).body["entries"][0]["note"], "only note")

    def test_every_allowed_category_and_action_is_accepted(self):
        for value in store.ALLOWED_CATEGORIES:
            self.assertEqual(self.create(category=value).status, 201, value)
        for value in store.ALLOWED_ACTIONS:
            self.assertEqual(self.create(action=value).status, 201, value)

    def test_invalid_category_and_action_through_the_api(self):
        for fields in ({"category": "bored"}, {"action": "meditated"}, {"category": 7}, {"action": ["distraction"]}):
            response = self.create(**fields)
            self.assertEqual(response.status, 422, fields)
        self.assertEqual(self.count("journal_entries"), 0)

    def test_note_limit(self):
        self.assertEqual(self.create(note="n" * 2000).status, 201)
        self.assertEqual(self.create(note="n" * 2001).status, 422)
        self.assertEqual(self.count("journal_entries"), 1)

    def test_list_is_newest_first_and_paginates_without_gaps_or_repeats(self):
        ids = self.seed_entries(ALICE, 60)
        seen, cursor, pages = [], None, 0
        while True:
            query = {"limit": "25"}
            if cursor:
                query["cursor"] = cursor
            body = self.call("entries_list", ALICE, query).body
            seen += [e["id"] for e in body["entries"]]
            pages += 1
            cursor = body["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(pages, 3)
        self.assertEqual(seen, list(reversed(ids)))

    def test_page_size_is_bounded_server_side(self):
        self.seed_entries(ALICE, 60)
        self.assertEqual(len(self.call("entries_list", ALICE, {"limit": "50"}).body["entries"]), 50)
        self.assertEqual(len(self.call("entries_list", ALICE, {}).body["entries"]), 20)
        for bad in ("51", "500", "0", "-3", "ten"):
            response = self.call("entries_list", ALICE, {"limit": bad})
            self.assertEqual(response.status, 422, bad)
            self.assertNotIn("entries", response.body)
        self.assertEqual(self.call("entries_list", ALICE, {"cursor": "garbage!"}).status, 422)

    def test_delete_all_removes_entries_and_profile(self):
        self.create(note="one")
        self.create(category="boredom")
        self.patch_profile(goal="goal", coping_actions=["walk"])
        response = self.call("delete_all", ALICE)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, {"status": "deleted", "entries_deleted": 2, "profile_deleted": True})
        self.assertEqual(self.count("journal_entries"), 0)
        self.assertEqual(self.count("journal_profiles"), 0)
        self.assertEqual(self.call("delete_all", ALICE).body["entries_deleted"], 0)

    def test_export_contains_everything_for_the_member_only(self):
        self.patch_profile(goal="Goal", why_it_matters="Why", coping_actions=["a", "b"])
        self.tick(minutes=1)
        first = self.create(category="boredom", note="first").body
        self.tick(minutes=1)
        second = self.create(action="distraction").body
        self.create(BOB, note="bob private")
        response = self.call("export", ALICE)
        self.assertEqual(response.status, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        body = response.body
        self.assertEqual(body["format"], "filtersight-journal-export")
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["profile"]["goal"], "Goal")
        self.assertEqual(body["profile"]["coping_actions"], ["a", "b"])
        self.assertEqual([e["id"] for e in body["entries"]], [second["id"], first["id"]])
        self.assertNotIn("bob private", json.dumps(body))
        json.dumps(body)  # machine-readable

    def test_all_responses_are_not_cacheable(self):
        self.assertEqual(self.call("profile_get", ALICE).headers["Cache-Control"], "no-store")
        self.assertEqual(self.call("export", ALICE).headers["Cache-Control"], "no-store")
        self.assertEqual(self.create(note="x").headers["Cache-Control"], "no-store")
        self.assertEqual(self.create(category="bad").headers["Cache-Control"], "no-store")


# ===========================================================================
# Isolation between members
# ===========================================================================
class IsolationTests(JournalTestCase):
    def test_members_cannot_see_or_touch_each_others_data(self):
        self.patch_profile(ALICE, goal="alice goal", coping_actions=["alice coping"])
        entry = self.create(ALICE, note="alice note", category="loneliness").body
        self.create(BOB, note="bob note")

        bob_list = self.call("entries_list", BOB, {}).body["entries"]
        self.assertEqual([e["note"] for e in bob_list], ["bob note"])
        self.assertIsNone(self.call("profile_get", BOB).body["goal"])

        stolen_update = self.service.entry_update(BOB, entry["id"], b'{"note": "hijacked"}')
        stolen_delete = self.call("entry_delete", BOB, entry["id"])
        missing_delete = self.call("entry_delete", BOB, "doesnotexist1234567890")
        self.assertEqual((stolen_update.status, stolen_delete.status), (404, 404))
        # Another member's id is indistinguishable from an id that does not exist.
        self.assertEqual(stolen_delete.body, missing_delete.body)

        alice_entries = self.call("entries_list", ALICE, {}).body["entries"]
        self.assertEqual([e["note"] for e in alice_entries], ["alice note"])

        export = json.dumps(self.call("export", BOB).body)
        for secret in ("alice note", "alice goal", "alice coping", entry["id"]):
            self.assertNotIn(secret, export)

        self.call("delete_all", BOB)
        self.assertEqual(self.call("entries_list", ALICE, {}).body["entries"][0]["note"], "alice note")
        self.assertEqual(self.call("profile_get", ALICE).body["goal"], "alice goal")

    def test_a_crafted_cursor_cannot_reach_another_members_rows(self):
        entry = self.create(ALICE, note="alice note").body
        self.create(BOB, note="bob note")
        cursor = store.encode_cursor("9999-01-01T00:00:00.000000Z", "zzzz")
        rows = self.call("entries_list", BOB, {"cursor": cursor}).body["entries"]
        self.assertEqual([e["note"] for e in rows], ["bob note"])
        self.assertNotIn(entry["id"], json.dumps(rows))

    def test_rate_limits_are_per_member(self):
        for _ in range(store.ENTRY_WRITES_PER_HOUR):
            self.assertEqual(self.create(ALICE, note="x").status, 201)
        self.assertEqual(self.create(ALICE, note="x").status, 429)
        self.assertEqual(self.create(BOB, note="x").status, 201)

    def test_the_member_identity_is_never_taken_from_the_request_body(self):
        for key in ("member_email", "email", "member", "entry_id", "id"):
            response = self.service.entry_create(ALICE, json.dumps({"note": "x", key: BOB}).encode())
            self.assertEqual(response.status, 422, key)


# ===========================================================================
# Encryption at rest
# ===========================================================================
class EncryptionAtRestTests(JournalTestCase):
    SENTINELS = {
        "goal": "SENTINELGOAL7731",
        "why": "SENTINELWHY8842",
        "coping1": "SENTINELCOPEONE91",
        "coping2": "SENTINELCOPETWO92",
        "coping3": "SENTINELCOPETHREE93",
        "note": "SENTINELNOTE5520",
    }

    def seed_everything(self):
        s = self.SENTINELS
        self.patch_profile(
            goal=s["goal"], why_it_matters=s["why"], coping_actions=[s["coping1"], s["coping2"], s["coping3"]]
        )
        self.create(category="stress_or_anxiety", action="talked_it_through", note=s["note"])

    def test_no_plaintext_sensitive_value_reaches_the_database(self):
        self.seed_everything()
        dump = self.dump_all_tables()
        raw = self.raw_db_bytes()
        for name, sentinel in self.SENTINELS.items():
            self.assertNotIn(sentinel, dump, name)
            self.assertNotIn(sentinel.encode(), raw, name)

    def test_sensitive_columns_hold_versioned_ciphertext(self):
        self.seed_everything()
        goal, why, coping = self.sql("SELECT goal_enc, why_enc, coping_enc FROM journal_profiles")[0]
        note = self.sql("SELECT note_enc FROM journal_entries")[0][0]
        for value in (goal, why, coping, note):
            self.assertTrue(value.startswith("v1:"))

    def test_only_documented_fields_are_plaintext(self):
        self.seed_everything()
        entry = self.sql("SELECT member_email, category, action, created_at FROM journal_entries")[0]
        self.assertEqual(entry[:3], (ALICE, "stress_or_anxiety", "talked_it_through"))

    def test_rewriting_the_same_value_produces_new_ciphertext(self):
        self.patch_profile(goal="same goal")
        first = self.sql("SELECT goal_enc FROM journal_profiles")[0][0]
        self.tick(minutes=1)
        self.patch_profile(goal="same goal")
        second = self.sql("SELECT goal_enc FROM journal_profiles")[0][0]
        self.assertNotEqual(first, second)

    def test_swapped_ciphertext_is_rejected(self):
        a = self.create(note="note A").body["id"]
        self.tick(minutes=1)
        b = self.create(note="note B").body["id"]
        conn = self.get_db()
        a_ct = conn.execute("SELECT note_enc FROM journal_entries WHERE entry_id = ?", (a,)).fetchone()[0]
        conn.execute("UPDATE journal_entries SET note_enc = ? WHERE entry_id = ?", (a_ct, b))
        conn.commit()
        conn.close()
        response = self.call("entries_list", ALICE, {})
        self.assertEqual(response.status, 503)
        self.assertNotIn("note A", json.dumps(response.body))

    def test_secure_delete_is_switched_on_for_every_write_transaction(self):
        conn = self.get_db()
        self.assertEqual(conn.execute("PRAGMA secure_delete").fetchone()[0], 0)
        with store.immediate_transaction(conn):
            pass
        self.assertEqual(conn.execute("PRAGMA secure_delete").fetchone()[0], 1)
        conn.close()

    def test_deleted_ciphertext_is_overwritten_not_left_in_free_pages(self):
        entry = self.create(note="to be erased").body
        ciphertext = self.sql("SELECT note_enc FROM journal_entries")[0][0]
        self.assertIn(ciphertext.encode(), self.raw_db_bytes())
        self.call("entry_delete", ALICE, entry["id"])
        self.assertNotIn(ciphertext.encode(), self.raw_db_bytes())


# ===========================================================================
# Fail closed when the key is missing, invalid or wrong
# ===========================================================================
class FailClosedTests(JournalTestCase):
    def every_call(self):
        return {
            "profile_get": self.call("profile_get", ALICE),
            "profile_update": self.service.profile_update(ALICE, b'{"goal": "x"}'),
            "entries_list": self.call("entries_list", ALICE, {}),
            "entry_create": self.service.entry_create(ALICE, b'{"note": "x"}'),
            "entry_update": self.service.entry_update(ALICE, "abc", b'{"note": "x"}'),
            "entry_delete": self.call("entry_delete", ALICE, "abc"),
            "delete_all": self.call("delete_all", ALICE),
            "export": self.call("export", ALICE),
        }

    def assertAllUnavailable(self, responses, forbidden=()):
        for name, response in responses.items():
            self.assertEqual(response.status, 503, name)
            self.assertEqual(response.body, {"detail": "The journal is not available right now."}, name)
            for text in forbidden:
                self.assertNotIn(text, json.dumps(response.body), name)

    def test_missing_key(self):
        self.get_db().close()  # production's get_db() creates the tables on every connection
        self.service = journal_service.JournalService(self.get_db, environ={}, clock=lambda: self.now)
        self.assertAllUnavailable(self.every_call())
        self.assertEqual(self.count("journal_entries"), 0)
        self.assertEqual(self.count("journal_profiles"), 0)
        self.assertEqual(self.count("journal_meta"), 0)

    def test_invalid_key_is_never_echoed(self):
        self.get_db().close()
        for bad in ("garbage-key-value", base64.urlsafe_b64encode(b"x" * 16).decode(), KEY.rstrip("=")):
            self.service = journal_service.JournalService(
                self.get_db, environ={store.KEY_ENV: bad}, clock=lambda: self.now
            )
            self.assertAllUnavailable(self.every_call(), forbidden=(bad,))
        self.assertEqual(self.count("journal_meta"), 0)

    def test_service_reads_the_key_from_the_process_environment(self):
        service = journal_service.JournalService(self.get_db, clock=lambda: self.now)
        with patch.dict(os.environ, {store.KEY_ENV: KEY}):
            self.assertEqual(service.profile_get(ALICE).status, 200)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(store.KEY_ENV, None)
            self.assertEqual(service.profile_get(ALICE).status, 503)

    def test_wrong_key_is_refused_and_existing_data_is_untouched(self):
        self.create(note="original note")
        self.patch_profile(goal="original goal")
        before = self.raw_db_bytes()
        self.service = journal_service.JournalService(
            self.get_db, environ={store.KEY_ENV: OTHER_KEY}, clock=lambda: self.now
        )
        self.assertAllUnavailable(self.every_call(), forbidden=("original", OTHER_KEY, KEY))
        self.assertEqual(self.raw_db_bytes(), before)
        # The right key still works afterwards.
        self.service = journal_service.JournalService(self.get_db, environ={store.KEY_ENV: KEY}, clock=lambda: self.now)
        self.assertEqual(self.call("entries_list", ALICE, {}).body["entries"][0]["note"], "original note")
        self.assertEqual(self.call("profile_get", ALICE).body["goal"], "original goal")

    def test_changing_the_key_is_allowed_only_while_no_data_exists(self):
        self.assertEqual(self.call("profile_get", ALICE).status, 200)  # seeds the key check
        other = journal_service.JournalService(self.get_db, environ={store.KEY_ENV: OTHER_KEY}, clock=lambda: self.now)
        self.assertEqual(other.profile_get(ALICE).status, 200)  # no data yet: re-seeded
        self.assertEqual(other.entry_create(ALICE, b'{"note": "x"}').status, 201)
        self.assertEqual(self.call("profile_get", ALICE).status, 503)  # data now exists

    def test_undecryptable_stored_value_fails_closed(self):
        self.create(note="x")
        conn = self.get_db()
        conn.execute("UPDATE journal_entries SET note_enc = 'v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'")
        conn.commit()
        conn.close()
        self.assertEqual(self.call("entries_list", ALICE, {}).status, 503)
        self.assertEqual(self.call("export", ALICE).status, 503)


# ===========================================================================
# Sanitized errors never echo input
# ===========================================================================
class SanitizedErrorTests(JournalTestCase):
    SECRET = "ZZSECRETINPUTZZ"

    def assertSanitized(self, response):
        self.assertIn(response.status, (404, 409, 413, 422, 429, 500, 503))
        text = json.dumps(response.body)
        self.assertNotIn(self.SECRET, text)
        self.assertNotIn(self.SECRET.lower(), text.lower())
        for leak in ("Traceback", "sqlite", "SELECT ", "InvalidTag", "File \""):
            self.assertNotIn(leak, text)

    def test_validation_errors_report_only_field_reason_and_limits(self):
        s = self.SECRET
        cases = [
            ("entry_create", (ALICE, json.dumps({"note": s + "a" * 2100}).encode())),
            ("entry_create", (ALICE, json.dumps({"category": s}).encode())),
            ("entry_create", (ALICE, json.dumps({"action": [s]}).encode())),
            ("entry_create", (ALICE, json.dumps({s: "value"}).encode())),
            ("entry_create", (ALICE, json.dumps({"note": f"visit {s}.com"}).encode())),
            ("entry_create", (ALICE, json.dumps({"note": 12345, "category": s}).encode())),
            ("entry_create", (ALICE, ('{"note": "' + s).encode())),
            ("entry_create", (ALICE, json.dumps([s]).encode())),
            ("profile_update", (ALICE, json.dumps({"goal": s * 100}).encode())),
            ("profile_update", (ALICE, json.dumps({"why_it_matters": s * 100}).encode())),
            ("profile_update", (ALICE, json.dumps({"coping_actions": [s * 100]}).encode())),
            ("profile_update", (ALICE, json.dumps({"coping_actions": [s, s, s, s]}).encode())),
            ("profile_update", (ALICE, json.dumps({"coping_actions": s}).encode())),
            ("profile_update", (ALICE, json.dumps({s: s}).encode())),
            ("entries_list", (ALICE, {"limit": s})),
            ("entries_list", (ALICE, {"cursor": s})),
        ]
        for method, args in cases:
            with self.subTest(method=method, args=str(args)[:60]):
                response = getattr(self.service, method)(*args)
                self.assertEqual(response.status, 422)
                self.assertSanitized(response)
                for item in response.body["detail"]:
                    self.assertTrue(set(item) <= {"field", "reason", "max_length", "max_items", "allowed", "min", "max"})

    def test_error_bodies_name_the_field_and_the_limit(self):
        response = self.create(note="n" * 2001)
        self.assertEqual(response.body, {"detail": [{"field": "note", "reason": "too_long", "max_length": 2000}]})
        response = self.create(category="nope")
        self.assertEqual(
            response.body,
            {"detail": [{"field": "category", "reason": "not_allowed", "allowed": list(store.ALLOWED_CATEGORIES)}]},
        )

    def test_unexpected_internal_failures_are_generic(self):
        with patch.object(store, "create_entry", side_effect=RuntimeError(self.SECRET)):
            response = self.create(note="hello")
        self.assertEqual(response.status, 500)
        self.assertSanitized(response)
        with patch.object(store, "get_profile", side_effect=sqlite3.OperationalError(self.SECRET)):
            response = self.call("profile_get", ALICE)
        self.assertEqual(response.status, 500)
        self.assertSanitized(response)

    def test_not_found_and_limit_responses_are_generic(self):
        self.assertSanitized(self.call("entry_delete", ALICE, self.SECRET))
        self.assertSanitized(self.service.entry_update(ALICE, self.SECRET, b'{"note": "x"}'))
        self.assertSanitized(self.service.payload_too_large())


# ===========================================================================
# Abuse controls
# ===========================================================================
class RateLimitTests(JournalTestCase):
    def test_twenty_entry_writes_per_hour(self):
        for index in range(20):
            self.tick(minutes=1)
            self.assertEqual(self.create(note=f"n{index}").status, 201, index)
        self.tick(minutes=1)
        response = self.create(note="one too many")
        self.assertEqual(response.status, 429)
        self.assertEqual(response.body["detail"]["reason"], "rate_limited")
        self.assertEqual(response.body["detail"]["limit"], 20)
        # The first write was 20 minutes ago (at +1, now +21), so it ages out in 40 minutes.
        self.assertEqual(response.headers["Retry-After"], str(40 * 60))
        self.assertEqual(self.count("journal_entries"), 20)
        self.tick(minutes=39)
        self.assertEqual(self.create(note="still inside the window").status, 429)
        self.tick(minutes=1)
        self.assertEqual(self.create(note="allowed again").status, 201)

    def test_updates_count_but_deletes_do_not(self):
        entry = self.create(note="base").body
        for index in range(19):
            self.tick(seconds=1)
            self.assertEqual(self.service.entry_update(ALICE, entry["id"], json.dumps({"note": f"v{index}"}).encode()).status, 200)
        self.tick(seconds=1)
        self.assertEqual(self.service.entry_update(ALICE, entry["id"], b'{"note": "blocked"}').status, 429)
        self.assertEqual(self.call("entry_delete", ALICE, entry["id"]).status, 200)
        self.assertEqual(self.call("delete_all", ALICE).status, 200)

    def test_failed_requests_do_not_consume_the_budget(self):
        for _ in range(30):
            self.assertEqual(self.create(category="invalid").status, 422)
        self.assertEqual(self.service.entry_update(ALICE, "nonexistent1234567890", b'{"note": "x"}').status, 404)
        self.assertEqual(self.count("journal_write_log"), 0)
        self.assertEqual(self.create(note="fine").status, 201)

    def test_ten_profile_updates_per_hour(self):
        for index in range(10):
            self.tick(minutes=1)
            self.assertEqual(self.patch_profile(goal=f"goal {index}").status, 200, index)
        self.tick(minutes=1)
        response = self.patch_profile(goal="too many")
        self.assertEqual(response.status, 429)
        self.assertEqual(response.body["detail"]["limit"], 10)
        self.assertEqual(self.call("profile_get", ALICE).body["goal"], "goal 9")
        self.assertEqual(self.create(note="entries are budgeted separately").status, 201)
        self.tick(hours=1)
        self.assertEqual(self.patch_profile(goal="after the window").status, 200)

    def test_delete_all_cannot_be_used_to_reset_the_limits(self):
        for _ in range(20):
            self.create(note="x")
        self.call("delete_all", ALICE)
        self.assertEqual(self.create(note="x").status, 429)

    def test_five_hundred_entry_cap(self):
        self.seed_entries(ALICE, 500)
        response = self.create(note="501st")
        self.assertEqual(response.status, 409)
        self.assertEqual(response.body, {"detail": {"reason": "entry_limit_reached", "limit": 500}})
        self.assertEqual(self.count("journal_entries", ALICE), 500)
        self.call("entry_delete", ALICE, "seed00000")
        self.assertEqual(self.create(note="fits now").status, 201)
        self.assertEqual(self.create(BOB, note="other members have their own cap").status, 201)


# ===========================================================================
# Retention
# ===========================================================================
class RetentionTests(JournalTestCase):
    ENDED = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)

    def setUp(self):
        super().setUp()
        self.add_member(ALICE, subscription="sub_alice", customer="cus_alice")
        self.add_member(BOB, subscription="sub_bob", customer="cus_bob")
        self.create(ALICE, note="alice note")
        self.patch_profile(ALICE, goal="alice goal")
        self.create(BOB, note="bob note")

    def end_subscription(self, customer="cus_alice", subscription="sub_alice", ended=None):
        conn = self.get_db()
        conn.execute("UPDATE customers SET active = 0 WHERE stripe_customer_id = ?", (customer,))
        scheduled = store.schedule_deletion_for_customer(conn, customer, subscription, ended or self.ENDED)
        conn.commit()
        conn.close()
        return scheduled

    def sweep(self, at):
        return store.run_cleanup(self.get_db, now=at)

    def retention_row(self, member=ALICE):
        rows = self.sql(
            "SELECT subscription_ended_at, delete_after, purged_at FROM journal_retention WHERE member_email = ?", (member,)
        )
        return rows[0] if rows else None

    def test_subscription_end_schedules_deletion_within_thirty_days(self):
        self.assertEqual(self.end_subscription(), 1)
        ended_at, delete_after, purged_at = self.retention_row()
        self.assertEqual(ended_at, store.to_ts(self.ENDED))
        self.assertEqual(delete_after, store.to_ts(self.ENDED + timedelta(days=29)))
        self.assertLessEqual(store.from_ts(delete_after), self.ENDED + timedelta(days=30))
        self.assertIsNone(purged_at)
        self.assertIsNone(self.retention_row(BOB))
        self.assertEqual(self.count("journal_entries", ALICE), 1)  # kept until the deadline

    def test_nothing_is_scheduled_without_data_or_for_an_older_subscription(self):
        self.call("delete_all", ALICE)
        self.assertEqual(self.end_subscription(), 0)
        self.assertIsNone(self.retention_row())

        self.create(ALICE, note="again")
        self.tick(hours=1)
        self.assertEqual(self.end_subscription(subscription="sub_some_older_one"), 0)
        self.assertIsNone(self.retention_row())

    def test_repeated_end_events_do_not_push_the_deadline_later(self):
        self.end_subscription()
        first = self.retention_row()
        self.end_subscription(ended=self.ENDED + timedelta(days=5))
        self.assertEqual(self.retention_row(), first)

    def test_data_is_deleted_automatically_when_due(self):
        self.end_subscription()
        before_due = self.ENDED + timedelta(days=29) - timedelta(seconds=1)
        self.assertEqual(self.sweep(before_due), {"scheduled": 0, "purged": 0})
        self.assertEqual(self.count("journal_entries", ALICE), 1)

        self.assertEqual(self.sweep(self.ENDED + timedelta(days=29))["purged"], 1)
        self.assertEqual(self.count("journal_entries", ALICE), 0)
        self.assertEqual(self.count("journal_profiles", ALICE), 0)
        self.assertEqual(self.count("journal_write_log", ALICE), 0)
        self.assertIsNotNone(self.retention_row()[2])
        # Other members are untouched.
        self.assertEqual(self.count("journal_entries", BOB), 1)
        # Idempotent.
        self.assertEqual(self.sweep(self.ENDED + timedelta(days=40))["purged"], 0)

    def test_purged_content_is_not_left_in_the_database_file(self):
        ciphertext = self.sql("SELECT note_enc FROM journal_entries WHERE member_email = ?", (ALICE,))[0][0]
        self.end_subscription()
        self.sweep(self.ENDED + timedelta(days=30))
        self.assertNotIn(ciphertext.encode(), self.raw_db_bytes())

    def test_members_without_a_schedule_are_reconciled_then_deleted(self):
        conn = self.get_db()
        conn.execute("UPDATE customers SET active = 0 WHERE email = ?", (ALICE,))  # webhook never scheduled it
        conn.commit()
        conn.close()
        self.assertEqual(self.sweep(self.ENDED), {"scheduled": 1, "purged": 0})
        self.assertEqual(self.retention_row()[1], store.to_ts(self.ENDED + timedelta(days=29)))
        self.assertEqual(self.sweep(self.ENDED + timedelta(days=29))["purged"], 1)
        self.assertEqual(self.count("journal_entries", ALICE), 0)
        self.assertEqual(self.count("journal_entries", BOB), 1)  # still active

    def test_data_for_an_unknown_member_is_scheduled_too(self):
        self.create("ghost@example.com", note="no customer row")
        self.assertEqual(self.sweep(self.ENDED)["scheduled"], 1)
        self.assertEqual(self.count("journal_retention"), 1)

    def test_reactivation_before_the_deadline_keeps_the_data(self):
        self.end_subscription()
        conn = self.get_db()
        result = store.clear_pending_deletion(conn, ALICE, now=self.ENDED + timedelta(days=10))
        conn.commit()
        conn.close()
        self.assertEqual(result, "cleared")
        self.assertIsNone(self.retention_row())
        self.assertEqual(self.sweep(self.ENDED + timedelta(days=60))["purged"], 0)

    def test_reactivation_cannot_rescue_a_deletion_that_is_already_due(self):
        self.end_subscription()
        conn = self.get_db()
        result = store.clear_pending_deletion(conn, ALICE, now=self.ENDED + timedelta(days=29, minutes=1))
        conn.commit()
        conn.close()
        self.assertEqual(result, "purged_overdue")
        self.assertEqual(self.count("journal_entries", ALICE), 0)

    def test_reactivation_does_not_undo_or_hide_a_deletion_that_already_ran(self):
        self.end_subscription()
        self.sweep(self.ENDED + timedelta(days=30))
        purged_at = self.retention_row()[2]
        conn = self.get_db()
        result = store.clear_pending_deletion(conn, ALICE, now=self.ENDED + timedelta(days=45))
        conn.commit()
        conn.close()
        self.assertEqual(result, "already_purged")
        self.assertEqual(self.retention_row()[2], purged_at)  # the record stays
        self.assertEqual(self.count("journal_entries", ALICE), 0)  # nothing is restored
        conn = self.get_db()
        conn.execute("UPDATE customers SET active = 1 WHERE email = ?", (ALICE,))
        conn.commit()
        conn.close()
        self.now = self.ENDED + timedelta(days=46)
        self.assertEqual(self.call("entries_list", ALICE, {}).body["entries"], [])
        self.assertEqual(self.create(ALICE, note="fresh start").status, 201)
        self.assertEqual(self.sweep(self.ENDED + timedelta(days=90))["purged"], 0)  # new data is not swept
        self.assertEqual(self.count("journal_entries", ALICE), 1)

    def test_a_second_subscription_end_schedules_new_data_again(self):
        self.end_subscription()
        self.sweep(self.ENDED + timedelta(days=30))
        self.now = self.ENDED + timedelta(days=46)
        self.create(ALICE, note="second life")
        later = self.ENDED + timedelta(days=100)
        self.assertEqual(self.end_subscription(ended=later), 1)
        self.assertEqual(self.retention_row()[2], None)
        self.sweep(later + timedelta(days=29))
        self.assertEqual(self.count("journal_entries", ALICE), 0)

    def test_user_can_delete_everything_immediately_while_signed_in(self):
        self.call("delete_all", ALICE)
        self.assertEqual(self.count("journal_entries", ALICE), 0)
        self.assertEqual(self.count("journal_profiles", ALICE), 0)

    def test_schedule_survives_a_restart(self):
        self.end_subscription()
        # A restart discards every in-memory object; only the database remains.
        del self.service
        later = self.ENDED + timedelta(days=31)
        fresh_service = journal_service.JournalService(self.get_db, environ=self.environ, clock=lambda: later)
        self.assertEqual(fresh_service.profile_get(BOB).status, 200)
        self.assertEqual(store.run_cleanup(self.get_db, now=later)["purged"], 1)
        self.assertEqual(self.count("journal_entries", ALICE), 0)

    def test_a_crash_mid_purge_rolls_back_and_the_next_sweep_finishes(self):
        self.end_subscription()
        real_purge = store.purge_member

        def crashing_purge(conn, member, now):
            conn.execute("DELETE FROM journal_entries WHERE member_email = ?", (member,))
            raise RuntimeError("simulated crash")

        with patch.object(store, "purge_member", crashing_purge):
            with self.assertRaises(RuntimeError):
                self.sweep(self.ENDED + timedelta(days=30))
        self.assertEqual(self.count("journal_entries", ALICE), 1)  # rolled back
        self.assertIsNone(self.retention_row()[2])
        self.assertEqual(self.sweep(self.ENDED + timedelta(days=30))["purged"], 1)
        self.assertEqual(self.count("journal_entries", ALICE), 0)
        self.assertIs(store.purge_member, real_purge)

    def test_cleanup_needs_no_encryption_key(self):
        self.end_subscription()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.sweep(self.ENDED + timedelta(days=30))["purged"], 1)

    def test_cleanup_prunes_old_rate_limit_history_only(self):
        self.assertGreater(self.count("journal_write_log", BOB), 0)
        self.sweep(self.now + timedelta(hours=1))
        self.assertGreater(self.count("journal_write_log", BOB), 0)
        self.sweep(self.now + timedelta(hours=3))
        self.assertEqual(self.count("journal_write_log"), 0)


class CleanupLifecycleTests(JournalTestCase):
    def test_start_runs_an_immediate_sweep_then_repeats_until_stopped(self):
        calls = []

        def fake_cleanup(get_db, now=None):
            calls.append(1)
            return {"scheduled": 0, "purged": 0}

        async def scenario():
            with patch.object(store, "run_cleanup", fake_cleanup):
                task = await journal_service.start_cleanup(self.get_db, interval=0.01)
                self.assertEqual(len(calls), 1)  # startup catch-up already happened
                await asyncio.sleep(0.15)
                await journal_service.stop_cleanup(task)
                stopped_at = len(calls)
                await asyncio.sleep(0.05)
                self.assertEqual(len(calls), stopped_at)
                self.assertGreaterEqual(stopped_at, 3)

        asyncio.run(scenario())

    def test_restart_catches_up_on_deletions_that_came_due_while_down(self):
        self.add_member(ALICE)
        self.create(ALICE, note="overdue")
        conn = self.get_db()
        conn.execute("UPDATE customers SET active = 0")
        store.schedule_deletion(conn, ALICE, datetime(2020, 1, 1, tzinfo=timezone.utc))
        conn.commit()
        conn.close()

        async def scenario():
            task = await journal_service.start_cleanup(self.get_db, interval=3600)
            await journal_service.stop_cleanup(task)

        asyncio.run(scenario())
        self.assertEqual(self.count("journal_entries", ALICE), 0)

    def test_a_failing_sweep_never_stops_the_loop_or_leaks_details(self):
        calls = []

        def broken_cleanup(get_db, now=None):
            calls.append(1)
            raise RuntimeError("SECRET-DETAIL")

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("filtersight.journal")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

        async def scenario():
            with patch.object(store, "run_cleanup", broken_cleanup):
                task = await journal_service.start_cleanup(self.get_db, interval=0.01)
                await asyncio.sleep(0.1)
                await journal_service.stop_cleanup(task)

        asyncio.run(scenario())
        self.assertGreaterEqual(len(calls), 3)
        self.assertIn("RuntimeError", stream.getvalue())
        self.assertNotIn("SECRET-DETAIL", stream.getvalue())


# ===========================================================================
# Logging
# ===========================================================================
class LoggingTests(JournalTestCase):
    def test_journal_content_never_appears_in_logs_or_output(self):
        secrets_ = ["LOGSENTINELGOAL", "LOGSENTINELWHY", "LOGSENTINELCOPE", "LOGSENTINELNOTE", "LOGSENTINELBAD"]
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.patch_profile(goal=secrets_[0], why_it_matters=secrets_[1], coping_actions=[secrets_[2]])
                entry = self.create(category="boredom", note=secrets_[3]).body
                self.service.entry_update(ALICE, entry["id"], json.dumps({"note": secrets_[3] + " edited"}).encode())
                self.create(note=secrets_[4] + "x" * 3000)  # oversized: rejected
                self.create(category=secrets_[4])  # invalid: rejected
                self.service.entry_create(ALICE, ('{"note": "' + secrets_[4]).encode())  # malformed
                self.call("entries_list", ALICE, {})
                self.call("export", ALICE)
                with patch.object(store, "create_entry", side_effect=RuntimeError(secrets_[4])):
                    self.create(note=secrets_[3])
                wrong = journal_service.JournalService(self.get_db, environ={store.KEY_ENV: OTHER_KEY}, clock=lambda: self.now)
                wrong.entries_list(ALICE, {})
                self.call("entry_delete", ALICE, entry["id"])
                self.call("delete_all", ALICE)
                store.run_cleanup(self.get_db, now=self.now + timedelta(days=90))
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        combined = stream.getvalue() + out.getvalue() + err.getvalue()
        self.assertTrue(combined, "expected some log output to inspect")
        for secret in secrets_ + [KEY, OTHER_KEY, ALICE]:
            self.assertNotIn(secret, combined)


# ===========================================================================
# Migration
# ===========================================================================
class MigrationTests(unittest.TestCase):
    def snapshot(self, path):
        conn = sqlite3.connect(path)
        try:
            legacy = ("customers", "member_magic_links", "member_sessions")
            schema = {
                name: conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]
                for name in legacy
            }
            rows = {name: conn.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall() for name in legacy}
            return schema, rows
        finally:
            conn.close()

    def test_production_shaped_database_migrates_additively_and_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "customers.db")
            create_production_shaped_database(path)
            conn = sqlite3.connect(path)
            conn.execute(
                """INSERT INTO customers (email, stripe_customer_id, stripe_subscription_id, active, plan,
                       nextdns_profile_id, removal_fee_paid, user_phone, partner_opt_in_status)
                   VALUES ('member@example.com', 'cus_1', 'sub_1', 1, 'filtersight', 'abc123', 0, NULL, NULL)"""
            )
            conn.execute("INSERT INTO customers (email, active, plan) VALUES ('old@example.com', 0, 'tier1')")
            conn.execute("INSERT INTO member_magic_links VALUES ('member@example.com', 'h', '2099-01-01', '2026-10-01')")
            conn.execute("INSERT INTO member_sessions VALUES ('th', 'member@example.com', 'sub_1', '2099-01-01', '2026-10-01')")
            conn.commit()
            conn.close()
            before = self.snapshot(path)

            for _ in range(3):  # idempotent: get_db runs on every request
                conn = sqlite3.connect(path)
                store.ensure_schema(conn)
                conn.commit()
                conn.close()

            self.assertEqual(self.snapshot(path), before)  # existing tables and rows untouched
            conn = sqlite3.connect(path)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_journal%'")}
            for table in store.JOURNAL_TABLES:
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            conn.close()
            self.assertEqual(
                tables,
                {"customers", "member_magic_links", "member_sessions"} | set(store.JOURNAL_TABLES),
            )
            self.assertEqual(
                indexes,
                {
                    "idx_journal_entries_member_created",
                    "idx_journal_write_log_member",
                    "idx_journal_retention_due",
                },
            )

    def test_migration_creates_only_new_objects(self):
        for statement in store._SCHEMA:
            self.assertRegex(statement, r"^\s*CREATE (TABLE|INDEX) IF NOT EXISTS journal_|^\s*CREATE INDEX IF NOT EXISTS idx_journal_")
            self.assertNotRegex(statement.upper(), r"\b(ALTER|DROP|DELETE|UPDATE)\b")


# ===========================================================================
# Structural guarantees
# ===========================================================================
class StructuralTests(unittest.TestCase):
    def imported_modules(self, filename):
        tree = ast.parse((ROOT / filename).read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        return names

    def test_journal_code_never_talks_to_the_network_or_an_ai_provider(self):
        forbidden = {"anthropic", "chatbot", "requests", "urllib", "httpx", "socket", "http", "openai", "webhook_server"}
        for filename in ("journal_store.py", "journal_service.py", "journal_router.py"):
            self.assertEqual(self.imported_modules(filename) & forbidden, set(), filename)

    def test_all_sql_in_the_store_is_parameterized(self):
        source = (ROOT / "journal_store.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "execute" and node.args:
                query = node.args[0]
                if isinstance(query, ast.JoinedStr):
                    # f-strings may only interpolate fixed module constants/column lists.
                    for part in query.values:
                        if isinstance(part, ast.FormattedValue):
                            self.assertIn(
                                ast.unparse(part.value),
                                {"_ENTRY_COLUMNS", "where"},
                                f"unexpected SQL interpolation: {ast.unparse(part.value)}",
                            )
                else:
                    self.assertNotIsInstance(query, ast.BinOp, "SQL built with + or %")


if __name__ == "__main__":
    unittest.main()
