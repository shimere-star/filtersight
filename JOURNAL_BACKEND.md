# Private journal backend

Backend only. No frontend, no changes to `app.py`, `companion_flow.py`, `index.html`, `privacy.html`, `terms.html`.

## Verification status (read first)
- Complete suite: **151 tests run and passing** on 2026-10-09 with `python -m unittest discover -s tests -v`.
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

Categories: craving_or_temptation, stress_or_anxiety, boredom, loneliness, accidental_block, legitimate_access_need, something_else.
Actions: breathing_exercise, grounding_exercise, distraction, five_minute_cooldown, ten_minute_cooldown, talked_it_through, contacted_trusted_person, did_something_else.

Errors: 422 `{"detail":[{"field","reason",...limit/allowed}]}` (never echoes input); 404, 409, 429 (+`Retry-After`), 413 (body >64 KiB), 503, 500 generic. URLs/domains/IPs in free text are rejected (heuristic, not perfect). Unknown fields rejected. All responses `Cache-Control: no-store`.

## Retention
- `customer.subscription.deleted` webhook records the end and schedules deletion at end + 29 days (30-day limit with a 1-day margin).
- Cleanup runs at app startup and every 15 minutes (in-process asyncio task). After a restart the startup sweep catches anything overdue. The sweep needs no encryption key. Each purge is its own transaction with `secure_delete` on; it also reconciles journal data of members with no active subscription.
- Reactivation before the deadline clears the pending deletion; if the deadline already passed, the data is purged first and not restored.
- Members can't sign in after the subscription ends, so the journal is inaccessible during the retention window (they can't use the in-app delete during it). Consider a support deletion path.
- Hooks are wrapped so a journal failure cannot break billing/webhooks.

## Frontend notes
Send `Authorization: Bearer <member session token>`. Treat 503 as "journal unavailable". Don't put URLs/domains in notes.

## Deploy prerequisites
Set `JOURNAL_ENCRYPTION_KEY` in Railway (not done here); add `cryptography` (in requirements.txt). Back the key up securely: losing it loses all journal data.

## Privacy/terms changes needed
State: what is stored, encrypted at rest, kept while subscribed, deleted within 30 days after ending, immediate delete-all and export, never sent to AI or other people, backups caveat, no URLs/browsing data accepted.
