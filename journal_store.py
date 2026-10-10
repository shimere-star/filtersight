"""Private goal / coping-plan / journal storage for FilterSight members.

This module is deliberately framework-free (stdlib + ``cryptography`` only) so
every rule that protects member privacy can be exercised without a web server.

Privacy model, in short:
  * Sensitive free text (goal, "why it matters", coping actions, journal notes)
    is encrypted with AES-256-GCM before it touches SQLite. The key comes only
    from the ``JOURNAL_ENCRYPTION_KEY`` environment variable.
  * Only the fixed-allowlist category/action values, timestamps and the member
    email are stored in plaintext. See JOURNAL_BACKEND.md for the full list.
  * Nothing in this module logs journal content, and error objects never carry
    submitted input.
  * Nothing here talks to a network or to an AI provider.
"""

import base64
import binascii
import contextlib
import datetime
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import unicodedata
from datetime import timedelta, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("filtersight.journal")

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------
KEY_ENV = "JOURNAL_ENCRYPTION_KEY"

ALLOWED_CATEGORIES = (
    "craving_or_temptation",
    "stress_or_anxiety",
    "boredom",
    "loneliness",
    "accidental_block",
    "legitimate_access_need",
    "something_else",
)
ALLOWED_ACTIONS = (
    "breathing_exercise",
    "grounding_exercise",
    "distraction",
    "five_minute_cooldown",
    "ten_minute_cooldown",
    "talked_it_through",
    "contacted_trusted_person",
    "did_something_else",
)

GOAL_MAX = 500
WHY_MAX = 1000
COPING_MAX_ITEMS = 3
COPING_ITEM_MAX = 250
NOTE_MAX = 2000

MAX_ENTRIES_PER_MEMBER = 500
ENTRY_WRITES_PER_HOUR = 20
PROFILE_WRITES_PER_HOUR = 10
RATE_WINDOW = timedelta(hours=1)

PAGE_DEFAULT = 20
PAGE_MAX = 50

# Retention: data is kept while the subscription is active. After it ends it is
# deleted no later than RETENTION_DAYS later. Deletion is *scheduled* one day
# early (PURGE_MARGIN) so sweep lag or a short outage still honours the promise.
RETENTION_DAYS = 30
PURGE_MARGIN = timedelta(days=1)

KIND_ENTRY = "entry"
KIND_PROFILE = "profile"

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KEY_CANARY = "filtersight-journal-key-check"


# ---------------------------------------------------------------------------
# Errors. None of these ever carry submitted input or key material.
# ---------------------------------------------------------------------------
class JournalError(Exception):
    pass


class JournalUnavailable(JournalError):
    """Key missing/invalid/mismatched, or stored data could not be decrypted."""


class JournalNotFound(JournalError):
    pass


class JournalLimitReached(JournalError):
    def __init__(self, limit):
        super().__init__("limit reached")
        self.limit = limit


class JournalRateLimited(JournalError):
    def __init__(self, limit, retry_after):
        super().__init__("rate limited")
        self.limit = limit
        self.retry_after = retry_after


class JournalValidationError(JournalError):
    """Carries only a field name, a generic reason and public limits."""

    def __init__(self, field, reason, **public_details):
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason
        self.public_details = public_details

    def as_dict(self):
        return {"field": self.field, "reason": self.reason, **self.public_details}


# ---------------------------------------------------------------------------
# Time helpers (fixed-width UTC strings so SQL string comparison sorts correctly)
# ---------------------------------------------------------------------------
def utcnow():
    return datetime.datetime.now(timezone.utc)


def to_ts(moment):
    return moment.astimezone(timezone.utc).strftime(_TS_FORMAT)


def from_ts(value):
    return datetime.datetime.strptime(value, _TS_FORMAT).replace(tzinfo=timezone.utc)


def timestamp_to_datetime(value, now=None):
    """Stripe-style unix seconds -> aware datetime; falls back to ``now``."""
    now = now or utcnow()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return now
    try:
        moment = datetime.datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return now
    return min(moment, now)


# ---------------------------------------------------------------------------
# Text validation and normalization
# ---------------------------------------------------------------------------
_STRIPPED_CODEPOINTS = frozenset(
    [0x200B, 0x200E, 0x200F, 0x2060, 0x061C, 0xFEFF]
    + list(range(0x202A, 0x202F))  # bidi embedding/override controls
    + list(range(0x2066, 0x206A))  # bidi isolates
)

_TLDS = (
    "com", "net", "org", "edu", "gov", "io", "co", "tv", "me", "xxx", "porn",
    "sex", "adult", "info", "biz", "app", "dev", "xyz", "site", "online",
    "club", "live", "link", "ly", "gg", "cc", "ws",
)
_WEB_ADDRESS_PATTERNS = (
    re.compile(r"\b[a-z][a-z0-9+.\-]{1,15}://", re.IGNORECASE),
    re.compile(r"\bwww\.", re.IGNORECASE),
    re.compile(
        r"(?<![A-Za-z0-9-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+(?:"
        + "|".join(_TLDS)
        + r")(?![A-Za-z0-9-])",
        re.IGNORECASE,
    ),
    re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
)


def contains_web_address(text):
    """Heuristic screen for URLs, domains and IPv4 addresses in free text.

    FilterSight must not store browsing history or sites, so these are
    rejected. This is a best-effort screen, not a guarantee.
    """
    return any(pattern.search(text) for pattern in _WEB_ADDRESS_PATTERNS)


def normalize_text(value, *, field, max_length, multiline=False):
    """Validate and normalize one free-text value.

    Returns the cleaned string, or ``None`` when it is empty after cleaning.
    Raises JournalValidationError (never echoing the input) when unacceptable.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise JournalValidationError(field, "must_be_text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise JournalValidationError(field, "invalid_characters") from None

    text = unicodedata.normalize("NFC", value).replace("\r\n", "\n")
    pieces = []
    for char in text:
        codepoint = ord(char)
        if char in ("\n", "\r", " ", " "):
            pieces.append("\n")
        elif char == "\t":
            pieces.append(" ")
        elif unicodedata.category(char) == "Cc" or codepoint in _STRIPPED_CODEPOINTS:
            continue
        elif unicodedata.category(char) == "Zs":
            pieces.append(" ")
        else:
            pieces.append(char)
    text = "".join(pieces)

    if multiline:
        text = re.sub(r" +", " ", text)
        text = re.sub(r" ?\n ?", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    else:
        text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return None
    if len(text) > max_length:
        raise JournalValidationError(field, "too_long", max_length=max_length)
    if contains_web_address(text):
        raise JournalValidationError(field, "web_address_not_allowed")
    return text


def normalize_coping_actions(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise JournalValidationError("coping_actions", "must_be_list")
    if len(value) > COPING_MAX_ITEMS:
        raise JournalValidationError(
            "coping_actions", "too_many_items", max_items=COPING_MAX_ITEMS
        )
    cleaned = []
    for item in value:
        text = normalize_text(
            item, field="coping_actions", max_length=COPING_ITEM_MAX
        )
        if text is not None:
            cleaned.append(text)
    return cleaned


# ---------------------------------------------------------------------------
# Payload parsing (hand-rolled so that errors can never echo submitted input)
# ---------------------------------------------------------------------------
PROFILE_FIELDS = ("goal", "why_it_matters", "coping_actions")
ENTRY_FIELDS = ("category", "action", "note")


def _no_duplicate_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _reject_constant(_name):
    raise ValueError("non-finite number")


def parse_json_object(raw):
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise JournalValidationError("body", "invalid_json") from None
    if not isinstance(parsed, dict):
        raise JournalValidationError("body", "must_be_object")
    return parsed


def _reject_unknown_fields(payload, allowed):
    # The offending key is attacker-controlled text, so it is never echoed.
    if any(key not in allowed for key in payload):
        raise JournalValidationError("request", "unknown_field")


def validate_profile_changes(payload):
    """Return only the fields the caller supplied, normalized (None = clear)."""
    _reject_unknown_fields(payload, PROFILE_FIELDS)
    if not any(name in payload for name in PROFILE_FIELDS):
        raise JournalValidationError("request", "no_fields_provided")
    changes = {}
    if "goal" in payload:
        changes["goal"] = normalize_text(payload["goal"], field="goal", max_length=GOAL_MAX)
    if "why_it_matters" in payload:
        changes["why_it_matters"] = normalize_text(
            payload["why_it_matters"],
            field="why_it_matters",
            max_length=WHY_MAX,
            multiline=True,
        )
    if "coping_actions" in payload:
        changes["coping_actions"] = normalize_coping_actions(payload["coping_actions"])
    return changes


def _validate_choice(value, field, allowed):
    if value is not None and (not isinstance(value, str) or value not in allowed):
        raise JournalValidationError(field, "not_allowed", allowed=list(allowed))
    return value


def validate_entry_changes(payload):
    """Return only the fields the caller supplied, normalized (None = clear)."""
    _reject_unknown_fields(payload, ENTRY_FIELDS)
    if not any(name in payload for name in ENTRY_FIELDS):
        raise JournalValidationError("request", "no_fields_provided")
    changes = {}
    if "category" in payload:
        changes["category"] = _validate_choice(payload["category"], "category", ALLOWED_CATEGORIES)
    if "action" in payload:
        changes["action"] = _validate_choice(payload["action"], "action", ALLOWED_ACTIONS)
    if "note" in payload:
        changes["note"] = normalize_text(
            payload["note"], field="note", max_length=NOTE_MAX, multiline=True
        )
    return changes


def parse_limit(raw):
    if raw is None:
        return PAGE_DEFAULT
    if not isinstance(raw, str) or not re.fullmatch(r"\d{1,4}", raw):
        raise JournalValidationError("limit", "out_of_range", min=1, max=PAGE_MAX)
    limit = int(raw)
    if not 1 <= limit <= PAGE_MAX:
        raise JournalValidationError("limit", "out_of_range", min=1, max=PAGE_MAX)
    return limit


def encode_cursor(created_at, entry_id):
    return base64.urlsafe_b64encode(f"{created_at}|{entry_id}".encode("ascii")).decode("ascii")


def decode_cursor(token):
    try:
        if not isinstance(token, str) or len(token) > 200:
            raise ValueError
        created_at, entry_id = base64.b64decode(
            token.encode("ascii"), altchars=b"-_", validate=True
        ).decode("ascii").split("|")
        if not _TS_RE.match(created_at) or not _ID_RE.match(entry_id):
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error):
        raise JournalValidationError("cursor", "invalid_cursor") from None
    return created_at, entry_id


# ---------------------------------------------------------------------------
# Field-level encryption (AES-256-GCM)
# ---------------------------------------------------------------------------
class FieldCipher:
    """Encrypts individual values. Each value is bound (as AAD) to its column
    and row so ciphertext cannot be moved to another field or row undetected.
    """

    VERSION = "v1"

    def __init__(self, key):
        self._aead = AESGCM(key)

    @staticmethod
    def _aad(field, row_id):
        return f"filtersight-journal|{FieldCipher.VERSION}|{field}|{row_id}".encode("utf-8")

    def encrypt(self, plaintext, *, field, row_id):
        nonce = os.urandom(12)
        sealed = self._aead.encrypt(nonce, plaintext.encode("utf-8"), self._aad(field, row_id))
        return f"{self.VERSION}:" + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")

    def decrypt(self, token, *, field, row_id):
        try:
            version, _, body = token.partition(":")
            if version != self.VERSION:
                raise ValueError
            blob = base64.urlsafe_b64decode(body.encode("ascii"))
            nonce, sealed = blob[:12], blob[12:]
            return self._aead.decrypt(nonce, sealed, self._aad(field, row_id)).decode("utf-8")
        except (InvalidTag, ValueError, UnicodeError, binascii.Error, AttributeError):
            raise JournalUnavailable("stored value could not be decrypted") from None


def load_cipher(environ=None):
    """Build the cipher from JOURNAL_ENCRYPTION_KEY or fail closed.

    The key must be the canonical URL-safe base64 form of exactly 32 random
    bytes (44 characters). Nothing about the key is ever included in errors.
    """
    environ = os.environ if environ is None else environ
    raw = (environ.get(KEY_ENV) or "").strip()
    if not raw:
        raise JournalUnavailable("journal key is not configured")
    try:
        key = base64.b64decode(raw.encode("ascii"), altchars=b"-_", validate=True)
        canonical = base64.urlsafe_b64encode(key).decode("ascii")
    except (ValueError, UnicodeError, binascii.Error):
        raise JournalUnavailable("journal key is not valid") from None
    if len(key) != 32 or canonical != raw:
        raise JournalUnavailable("journal key is not valid")
    return FieldCipher(key)


# ---------------------------------------------------------------------------
# Schema (additive only: new tables and indexes, no change to existing ones)
# ---------------------------------------------------------------------------
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS journal_profiles (
           member_email TEXT PRIMARY KEY,
           goal_enc TEXT,
           why_enc TEXT,
           coping_enc TEXT,
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS journal_entries (
           entry_id TEXT PRIMARY KEY,
           member_email TEXT NOT NULL,
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL,
           category TEXT,
           action TEXT,
           note_enc TEXT
       )""",
    """CREATE INDEX IF NOT EXISTS idx_journal_entries_member_created
           ON journal_entries (member_email, created_at DESC, entry_id DESC)""",
    """CREATE TABLE IF NOT EXISTS journal_write_log (
           id INTEGER PRIMARY KEY,
           member_email TEXT NOT NULL,
           kind TEXT NOT NULL,
           written_at TEXT NOT NULL
       )""",
    """CREATE INDEX IF NOT EXISTS idx_journal_write_log_member
           ON journal_write_log (member_email, kind, written_at)""",
    """CREATE TABLE IF NOT EXISTS journal_retention (
           member_email TEXT PRIMARY KEY,
           subscription_ended_at TEXT NOT NULL,
           delete_after TEXT NOT NULL,
           purged_at TEXT
       )""",
    """CREATE INDEX IF NOT EXISTS idx_journal_retention_due
           ON journal_retention (purged_at, delete_after)""",
    """CREATE TABLE IF NOT EXISTS journal_meta (
           key TEXT PRIMARY KEY,
           value TEXT NOT NULL
       )""",
)

JOURNAL_TABLES = frozenset(
    {
        "journal_profiles",
        "journal_entries",
        "journal_write_log",
        "journal_retention",
        "journal_meta",
    }
)


def ensure_schema(conn):
    """Create the journal tables if missing. The caller commits."""
    for statement in _SCHEMA:
        conn.execute(statement)


@contextlib.contextmanager
def immediate_transaction(conn):
    """Take the write lock up front so check-then-write sequences are atomic."""
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


# ---------------------------------------------------------------------------
# Key consistency check
# ---------------------------------------------------------------------------
def _has_any_journal_data(conn):
    return bool(
        conn.execute("SELECT 1 FROM journal_entries LIMIT 1").fetchone()
        or conn.execute("SELECT 1 FROM journal_profiles LIMIT 1").fetchone()
    )


def verify_key(conn, cipher):
    """Refuse to run with a key that differs from the one that wrote the data.

    A sealed canary is stored on first use. If a later key cannot open it, all
    journal operations fail closed, so two keys can never silently split data.
    If no journal data exists at all, the canary is simply re-seeded.
    """
    row = conn.execute("SELECT value FROM journal_meta WHERE key = 'key_check'").fetchone()
    if row is not None:
        try:
            if cipher.decrypt(row[0], field="meta.key_check", row_id="key_check") == _KEY_CANARY:
                return
        except JournalUnavailable:
            pass
        if _has_any_journal_data(conn):
            raise JournalUnavailable("journal key does not match stored data")
        conn.execute("DELETE FROM journal_meta WHERE key = 'key_check'")
    sealed = cipher.encrypt(_KEY_CANARY, field="meta.key_check", row_id="key_check")
    conn.execute("INSERT OR IGNORE INTO journal_meta (key, value) VALUES ('key_check', ?)", (sealed,))
    conn.commit()
    row = conn.execute("SELECT value FROM journal_meta WHERE key = 'key_check'").fetchone()
    if cipher.decrypt(row[0], field="meta.key_check", row_id="key_check") != _KEY_CANARY:
        raise JournalUnavailable("journal key does not match stored data")


# ---------------------------------------------------------------------------
# Rate limiting (server-side, rolling one-hour window, recorded in SQLite)
# ---------------------------------------------------------------------------
def _check_rate(conn, member, kind, limit, now):
    cutoff = to_ts(now - RATE_WINDOW)
    rows = conn.execute(
        """SELECT written_at FROM journal_write_log
           WHERE member_email = ? AND kind = ? AND written_at > ?
           ORDER BY written_at ASC""",
        (member, kind, cutoff),
    ).fetchall()
    if len(rows) >= limit:
        frees_at = from_ts(rows[0][0]) + RATE_WINDOW
        retry_after = max(1, math.ceil((frees_at - now).total_seconds()))
        raise JournalRateLimited(limit, retry_after)


def _record_write(conn, member, kind, now):
    conn.execute(
        "INSERT INTO journal_write_log (member_email, kind, written_at) VALUES (?, ?, ?)",
        (member, kind, to_ts(now)),
    )


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
def _empty_profile():
    return {"goal": None, "why_it_matters": None, "coping_actions": [], "updated_at": None}


def _read_profile(conn, cipher, member):
    row = conn.execute(
        "SELECT goal_enc, why_enc, coping_enc, updated_at FROM journal_profiles WHERE member_email = ?",
        (member,),
    ).fetchone()
    if row is None:
        return _empty_profile(), False
    goal_enc, why_enc, coping_enc, updated_at = row
    coping = []
    if coping_enc:
        try:
            coping = json.loads(cipher.decrypt(coping_enc, field="profile.coping", row_id=member))
        except ValueError:
            raise JournalUnavailable("stored value could not be decrypted") from None
    return {
        "goal": cipher.decrypt(goal_enc, field="profile.goal", row_id=member) if goal_enc else None,
        "why_it_matters": cipher.decrypt(why_enc, field="profile.why", row_id=member) if why_enc else None,
        "coping_actions": coping,
        "updated_at": updated_at,
    }, True


def get_profile(conn, cipher, member):
    return _read_profile(conn, cipher, member)[0]


def update_profile(conn, cipher, member, changes, now):
    """Merge ``changes`` into the member's profile (omitted = unchanged)."""
    with immediate_transaction(conn):
        _check_rate(conn, member, KIND_PROFILE, PROFILE_WRITES_PER_HOUR, now)
        current, existed = _read_profile(conn, cipher, member)
        merged = {
            "goal": changes["goal"] if "goal" in changes else current["goal"],
            "why_it_matters": changes["why_it_matters"] if "why_it_matters" in changes else current["why_it_matters"],
            "coping_actions": changes["coping_actions"] if "coping_actions" in changes else current["coping_actions"],
        }
        stamp = to_ts(now)
        if not merged["goal"] and not merged["why_it_matters"] and not merged["coping_actions"]:
            conn.execute("DELETE FROM journal_profiles WHERE member_email = ?", (member,))
            updated_at = None
        else:
            goal_enc = cipher.encrypt(merged["goal"], field="profile.goal", row_id=member) if merged["goal"] else None
            why_enc = cipher.encrypt(merged["why_it_matters"], field="profile.why", row_id=member) if merged["why_it_matters"] else None
            coping_enc = (
                cipher.encrypt(json.dumps(merged["coping_actions"]), field="profile.coping", row_id=member)
                if merged["coping_actions"]
                else None
            )
            created_at = conn.execute(
                "SELECT created_at FROM journal_profiles WHERE member_email = ?", (member,)
            ).fetchone()
            conn.execute(
                """INSERT OR REPLACE INTO journal_profiles
                   (member_email, goal_enc, why_enc, coping_enc, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (member, goal_enc, why_enc, coping_enc, created_at[0] if created_at else stamp, stamp),
            )
            updated_at = stamp
        _record_write(conn, member, KIND_PROFILE, now)
    merged["updated_at"] = updated_at
    return merged


# ---------------------------------------------------------------------------
# Journal entries
# ---------------------------------------------------------------------------
def _entry_from_row(cipher, row):
    entry_id, created_at, updated_at, category, action, note_enc = row
    return {
        "id": entry_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "category": category,
        "action": action,
        "note": cipher.decrypt(note_enc, field="entry.note", row_id=entry_id) if note_enc else None,
    }


_ENTRY_COLUMNS = "entry_id, created_at, updated_at, category, action, note_enc"


def create_entry(conn, cipher, member, changes, now):
    category = changes.get("category")
    action = changes.get("action")
    note = changes.get("note")
    if category is None and action is None and note is None:
        raise JournalValidationError("entry", "must_have_content")
    entry_id = secrets.token_urlsafe(16)
    stamp = to_ts(now)
    with immediate_transaction(conn):
        count = conn.execute(
            "SELECT COUNT(*) FROM journal_entries WHERE member_email = ?", (member,)
        ).fetchone()[0]
        if count >= MAX_ENTRIES_PER_MEMBER:
            raise JournalLimitReached(MAX_ENTRIES_PER_MEMBER)
        _check_rate(conn, member, KIND_ENTRY, ENTRY_WRITES_PER_HOUR, now)
        conn.execute(
            f"INSERT INTO journal_entries ({_ENTRY_COLUMNS}, member_email) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entry_id,
                stamp,
                stamp,
                category,
                action,
                cipher.encrypt(note, field="entry.note", row_id=entry_id) if note else None,
                member,
            ),
        )
        _record_write(conn, member, KIND_ENTRY, now)
    return {
        "id": entry_id,
        "created_at": stamp,
        "updated_at": stamp,
        "category": category,
        "action": action,
        "note": note,
    }


def list_entries(conn, cipher, member, limit, cursor=None):
    params = [member]
    where = "member_email = ?"
    if cursor is not None:
        created_at, entry_id = decode_cursor(cursor)
        where += " AND (created_at < ? OR (created_at = ? AND entry_id < ?))"
        params += [created_at, created_at, entry_id]
    rows = conn.execute(
        f"""SELECT {_ENTRY_COLUMNS} FROM journal_entries
            WHERE {where}
            ORDER BY created_at DESC, entry_id DESC LIMIT ?""",
        (*params, limit + 1),
    ).fetchall()
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1][1], page[-1][0]) if len(rows) > limit else None
    return [_entry_from_row(cipher, row) for row in page], next_cursor


def update_entry(conn, cipher, member, entry_id, changes, now):
    if not _ID_RE.match(entry_id or ""):
        raise JournalNotFound("entry not found")
    with immediate_transaction(conn):
        row = conn.execute(
            f"SELECT {_ENTRY_COLUMNS} FROM journal_entries WHERE entry_id = ? AND member_email = ?",
            (entry_id, member),
        ).fetchone()
        if row is None:
            raise JournalNotFound("entry not found")
        current = _entry_from_row(cipher, row)
        merged = {name: changes[name] if name in changes else current[name] for name in ENTRY_FIELDS}
        if merged["category"] is None and merged["action"] is None and merged["note"] is None:
            raise JournalValidationError("entry", "must_have_content")
        _check_rate(conn, member, KIND_ENTRY, ENTRY_WRITES_PER_HOUR, now)
        stamp = to_ts(now)
        conn.execute(
            """UPDATE journal_entries
               SET category = ?, action = ?, note_enc = ?, updated_at = ?
               WHERE entry_id = ? AND member_email = ?""",
            (
                merged["category"],
                merged["action"],
                cipher.encrypt(merged["note"], field="entry.note", row_id=entry_id) if merged["note"] else None,
                stamp,
                entry_id,
                member,
            ),
        )
        _record_write(conn, member, KIND_ENTRY, now)
    return {
        "id": entry_id,
        "created_at": current["created_at"],
        "updated_at": stamp,
        "category": merged["category"],
        "action": merged["action"],
        "note": merged["note"],
    }


def delete_entry(conn, member, entry_id):
    """Deleting never counts toward write limits: members can always erase."""
    if not _ID_RE.match(entry_id or ""):
        raise JournalNotFound("entry not found")
    with immediate_transaction(conn):
        removed = conn.execute(
            "DELETE FROM journal_entries WHERE entry_id = ? AND member_email = ?",
            (entry_id, member),
        ).rowcount
    if not removed:
        raise JournalNotFound("entry not found")


def delete_all(conn, member):
    """Erase every entry and the profile. The write log is kept on purpose so
    deleting everything cannot be used to reset the hourly write limits."""
    with immediate_transaction(conn):
        entries = conn.execute("DELETE FROM journal_entries WHERE member_email = ?", (member,)).rowcount
        profile = conn.execute("DELETE FROM journal_profiles WHERE member_email = ?", (member,)).rowcount
    return {"entries_deleted": entries, "profile_deleted": bool(profile)}


def export_all(conn, cipher, member, now):
    rows = conn.execute(
        f"""SELECT {_ENTRY_COLUMNS} FROM journal_entries
            WHERE member_email = ? ORDER BY created_at DESC, entry_id DESC""",
        (member,),
    ).fetchall()
    return {
        "format": "filtersight-journal-export",
        "version": 1,
        "exported_at": to_ts(now),
        "profile": get_profile(conn, cipher, member),
        "entries": [_entry_from_row(cipher, row) for row in rows],
    }


# ---------------------------------------------------------------------------
# Retention. Helpers that take a caller's open transaction never commit.
# ---------------------------------------------------------------------------
def has_data(conn, member):
    return bool(
        conn.execute("SELECT 1 FROM journal_entries WHERE member_email = ? LIMIT 1", (member,)).fetchone()
        or conn.execute("SELECT 1 FROM journal_profiles WHERE member_email = ? LIMIT 1", (member,)).fetchone()
    )


def schedule_deletion(conn, member, ended_at):
    """Mark a member's journal data for deletion after their subscription ends.

    Returns True if a schedule now exists. A pending schedule is never pushed
    later by a repeated event; a schedule that has already executed is replaced.
    """
    if not has_data(conn, member):
        return False
    delete_after = ended_at + timedelta(days=RETENTION_DAYS) - PURGE_MARGIN
    conn.execute(
        """INSERT INTO journal_retention (member_email, subscription_ended_at, delete_after, purged_at)
           VALUES (?, ?, ?, NULL)
           ON CONFLICT(member_email) DO UPDATE SET
               subscription_ended_at = excluded.subscription_ended_at,
               delete_after = excluded.delete_after,
               purged_at = NULL
           WHERE journal_retention.purged_at IS NOT NULL""",
        (member, to_ts(ended_at), to_ts(delete_after)),
    )
    return True


def schedule_deletion_for_customer(conn, stripe_customer_id, subscription_id, ended_at):
    """Schedule deletion for the member(s) whose subscription just ended.

    A deletion event for an older subscription (different id) is ignored so a
    returning member is never scheduled by their previous subscription.
    """
    scheduled = 0
    rows = conn.execute(
        "SELECT email, stripe_subscription_id FROM customers WHERE stripe_customer_id = ?",
        (stripe_customer_id,),
    ).fetchall()
    for email, current_subscription in rows:
        if subscription_id and current_subscription and current_subscription != subscription_id:
            continue
        if schedule_deletion(conn, email, ended_at):
            scheduled += 1
    return scheduled


def purge_member(conn, member, now):
    """Delete all journal/profile data and rate-limit history for a member and
    record that the purge happened. The caller owns the transaction."""
    conn.execute("DELETE FROM journal_entries WHERE member_email = ?", (member,))
    conn.execute("DELETE FROM journal_profiles WHERE member_email = ?", (member,))
    conn.execute("DELETE FROM journal_write_log WHERE member_email = ?", (member,))
    stamp = to_ts(now)
    conn.execute(
        """INSERT INTO journal_retention (member_email, subscription_ended_at, delete_after, purged_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(member_email) DO UPDATE SET purged_at = excluded.purged_at""",
        (member, stamp, stamp, stamp),
    )


def clear_pending_deletion(conn, member, now=None):
    """Called when a member's subscription becomes active again.

    * pending and not yet due -> the schedule is cleared (their data is kept);
    * pending but already due -> it is deleted now, reactivation cannot rescue it;
    * already executed       -> the record stays, nothing is restored.
    """
    now = now or utcnow()
    row = conn.execute(
        "SELECT delete_after, purged_at FROM journal_retention WHERE member_email = ?", (member,)
    ).fetchone()
    if row is None:
        return "none"
    delete_after, purged_at = row
    if purged_at is not None:
        return "already_purged"
    if delete_after <= to_ts(now):
        purge_member(conn, member, now)
        return "purged_overdue"
    conn.execute(
        "DELETE FROM journal_retention WHERE member_email = ? AND purged_at IS NULL", (member,)
    )
    return "cleared"


def run_cleanup(get_db, now=None):
    """One retention sweep. Safe to run at any time, repeatedly, from any process.

    All state lives in SQLite, so a restart loses nothing: the next sweep simply
    finds anything overdue. Each member is purged in its own transaction.
    Needs no encryption key (it only deletes rows).
    """
    now = now or utcnow()
    scheduled = purged = 0
    conn = get_db()
    try:
        with immediate_transaction(conn):
            # Journal data whose owner is inactive (or unknown) but that has no
            # schedule yet, e.g. a missed webhook: schedule from now.
            orphans = conn.execute(
                """SELECT DISTINCT d.member_email
                   FROM (SELECT member_email FROM journal_entries
                         UNION SELECT member_email FROM journal_profiles) AS d
                   LEFT JOIN customers c ON c.email = d.member_email
                   LEFT JOIN journal_retention r ON r.member_email = d.member_email
                   WHERE (c.email IS NULL OR c.active = 0) AND r.member_email IS NULL"""
            ).fetchall()
            for (member,) in orphans:
                if schedule_deletion(conn, member, now):
                    scheduled += 1
        due = conn.execute(
            "SELECT member_email FROM journal_retention WHERE purged_at IS NULL AND delete_after <= ?",
            (to_ts(now),),
        ).fetchall()
        for (member,) in due:
            with immediate_transaction(conn):
                still_due = conn.execute(
                    """SELECT 1 FROM journal_retention
                       WHERE member_email = ? AND purged_at IS NULL AND delete_after <= ?""",
                    (member, to_ts(now)),
                ).fetchone()
                if still_due:
                    purge_member(conn, member, now)
                    purged += 1
        with immediate_transaction(conn):
            conn.execute(
                "DELETE FROM journal_write_log WHERE written_at <= ?",
                (to_ts(now - 2 * RATE_WINDOW),),
            )
    finally:
        conn.close()
    logger.info("journal.cleanup scheduled=%d purged=%d", scheduled, purged)
    return {"scheduled": scheduled, "purged": purged}
