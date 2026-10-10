# Private journal backend

The backend, member-facing Streamlit workspace, and matching Privacy/Terms copy are implemented on this branch. Production deployment and environment configuration are intentionally separate.

## Verification status (read first)
- Complete suite: **162 tests run and passing** on 2026-10-09 with `.venv/bin/python -m unittest discover -s tests -v`.
- Coverage includes the framework-free store/service rules, real FastAPI routes through `TestClient`, Stripe webhook retention hooks, startup cleanup, production-shaped migration, and the existing launch-plan regression tests.
- `compileall` and `git diff --check` also pass. Production deployment and live-service checks have not been performed.

## Encryption
- AES-256-GCM per field (goal, why, each coping list, note). Ciphertext `v1:` + urlsafe-b64(nonce(12) + ciphertext+tag).
- AAD = `filtersight-journal|v1|<field>|<row id>` (member email for profile, entry id for entries), so values can't be swapped between rows/fields.
- Env var `JOURNAL_ENCRYPTION_KEY`: 44-char URL-safe base64 of 32 random bytes. Generate locally, never in prod code:
  `python3 -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`
- Missing/invalid key: journal endpoints return 503 `{"detail":"The journal is not available right now."}`; the rest of the app is unaffected. A key canary in `journal_meta` makes a *wrong* key fail closed instead of corrupting data.
- **Rotation is not supported.** Changing the key makes existing journal data unreadable (fails closed). Only safe change when no journal data exists.
- Visible in a DB-only leak: member email, entry ids, created/updated timestamps, category, action, ciphertext lengths (approximate text length), retention dates, write-log timestamps. Hidden: goal, why, coping actions, notes.
- Railway volume backups are outside this feature's control; deleted rows can persist in platform snapshots. Disclose in privacy copy.

## Schema (additive, `CREATE TABLE IF NOT EXISTS`, run from `get_db()`)
`journal_profiles`, `journal_entries`, `journal_write_log`, `journal_retention`, `journal_meta`. No existing table is altered.

## Endpoints (member Bearer session; prefix `/member/journal`)
| Method | Path | Notes |
|---|---|---|
| GET/PATCH | `/profile` | PATCH: omitted = unchanged, null = clear. goal ≤500, why ≤1000, ≤3 coping actions ≤250 each. 10 updates/hr |
| GET | `/entries?limit=&cursor=` | newest first, limit default 20 max 50, opaque cursor, returns `next_cursor` |
| POST | `/entries` | `category`, `action`, `note` (≤2000); at least one required. 20 writes/hr, max 500 entries (409) |
| PATCH/DELETE | `/entries/{id}` | 404 for missing or other member's entry |
| DELETE | `/data` | deletes all entries + profile immediately |
| GET | `/export` | all data as JSON |

Support deletion for a member who can no longer sign in uses `POST /admin/journal/delete` with JSON `{"email":"subscription@example.com"}` and the `X-Journal-Admin-Secret` header. It returns the same `{"status":"processed"}` response whether or not journal data existed, works without the encryption key, and deletes journal/profile content plus journal write history. Support must verify the request before calling it. This endpoint does not delete the customer, billing, DNS-profile, or other account records.

Categories: craving_or_temptation, stress_or_anxiety, boredom, loneliness, accidental_block, legitimate_access_need, something_else.
Actions: breathing_exercise, grounding_exercise, distraction, five_minute_cooldown, ten_minute_cooldown, talked_it_through, contacted_trusted_person, did_something_else.

Errors: 422 `{"detail":[{"field","reason",...limit/allowed}]}` (never echoes input); 404, 409, 429 (+`Retry-After`), 413 (body >64 KiB), 503, 500 generic. URLs/domains/IPs in free text are rejected (heuristic, not perfect). Unknown fields rejected. All responses `Cache-Control: no-store`.

## Retention
- `customer.subscription.deleted` webhook records the end and schedules deletion at end + 29 days (30-day limit with a 1-day margin).
- Cleanup runs at app startup and every 15 minutes (in-process asyncio task). After a restart the startup sweep catches anything overdue. The sweep needs no encryption key. Each purge is its own transaction with `secure_delete` on; it also reconciles journal data of members with no active subscription.
- Reactivation before the deadline clears the pending deletion; if the deadline already passed, the data is purged first and not restored.
- Members can't sign in after the subscription ends, so the journal is inaccessible during the retention window. A protected support deletion endpoint is available for a verified request from the subscription email address.
- Hooks are wrapped so a journal failure cannot break billing/webhooks.

## Frontend
The member dashboard links to a single-column private-journal workspace. It supports the personal goal and coping plan, note-only or categorized entries, newest-first entry editing/deletion, JSON export, and confirmed delete-all. It sends `Authorization: Bearer <member session token>`, treats 503 as temporarily unavailable, and tells members not to enter URLs or domains.

## Deploy prerequisites
Set both `JOURNAL_ENCRYPTION_KEY` and a separate high-entropy `JOURNAL_ADMIN_SECRET` in Railway (not done here); `cryptography` is in `requirements.txt`. Back the encryption key up securely: losing it loses all journal data. The admin secret protects only the support deletion route and must not be reused for member or general admin access.

## Privacy/terms
`privacy.html` and `terms.html` now state what is stored, which fields are encrypted at rest, which operational metadata remains readable, active-subscription retention and deletion within 30 days after ending, immediate member delete/export controls, the verified support deletion path, the backups caveat, no automatic AI sharing, and the ban on URLs/browsing data in journal fields.
