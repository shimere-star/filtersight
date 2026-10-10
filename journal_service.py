"""Request-level logic for the private journal API.

Framework-free: every public method takes plain Python values and returns a
``Response`` (status, JSON-able body, headers). The thin FastAPI adapter in
``journal_router.py`` only moves bytes in and out, so all privacy rules live
here where they can be tested without a web stack.

Rules enforced here:
  * every failure becomes a small, generic body - no submitted input, key
    material, SQL, exception text or stack trace is ever returned;
  * nothing about journal content is ever logged (only event names, error
    class names, counts);
  * the journal fails closed (503) if the encryption key is absent, invalid or
    does not match the key that wrote the stored data;
  * nothing is ever sent to an AI provider from this layer.
"""

import asyncio
import contextlib
import logging
import sqlite3
from dataclasses import dataclass, field

import journal_store as store

logger = logging.getLogger("filtersight.journal")

MAX_BODY_BYTES = 65536
CLEANUP_INTERVAL_SECONDS = 900

_NO_STORE = {"Cache-Control": "no-store"}


@dataclass
class Response:
    status: int
    body: object
    headers: dict = field(default_factory=dict)


def _respond(status, body, headers=None):
    return Response(status, body, {**_NO_STORE, **(headers or {})})


class JournalService:
    max_body_bytes = MAX_BODY_BYTES

    def __init__(self, get_db, *, environ=None, clock=None):
        """``get_db`` returns a fresh SQLite connection with the schema ready.
        ``environ`` defaults to os.environ, read at call time (never cached)."""
        self._get_db = get_db
        self._environ = environ
        self._clock = clock or store.utcnow

    # -- plumbing ----------------------------------------------------------
    def payload_too_large(self):
        return _respond(413, {"detail": {"reason": "payload_too_large", "max_bytes": MAX_BODY_BYTES}})

    def _run(self, operation):
        """Open the DB, check the key, run ``operation`` and map every failure
        to a generic response."""
        try:
            cipher = store.load_cipher(self._environ)
            now = self._clock()
            conn = self._get_db()
            try:
                store.verify_key(conn, cipher)
                return operation(conn, cipher, now)
            finally:
                conn.close()
        except store.JournalValidationError as error:
            return _respond(422, {"detail": [error.as_dict()]})
        except store.JournalNotFound:
            return _respond(404, {"detail": "Not found"})
        except store.JournalLimitReached as error:
            return _respond(
                409, {"detail": {"reason": "entry_limit_reached", "limit": error.limit}}
            )
        except store.JournalRateLimited as error:
            return _respond(
                429,
                {
                    "detail": {
                        "reason": "rate_limited",
                        "limit": error.limit,
                        "window_seconds": int(store.RATE_WINDOW.total_seconds()),
                        "retry_after_seconds": error.retry_after,
                    }
                },
                {"Retry-After": str(error.retry_after)},
            )
        except store.JournalUnavailable:
            logger.warning("journal.unavailable")
            return _respond(503, {"detail": "The journal is not available right now."})
        except sqlite3.Error as error:
            logger.error("journal.database_error error=%s", type(error).__name__)
            return _respond(500, {"detail": "Something went wrong. Please try again."})
        except Exception as error:  # last resort: never leak details
            logger.error("journal.unexpected_error error=%s", type(error).__name__)
            return _respond(500, {"detail": "Something went wrong. Please try again."})

    # -- profile -----------------------------------------------------------
    def profile_get(self, member):
        return self._run(lambda conn, cipher, now: _respond(200, store.get_profile(conn, cipher, member)))

    def profile_update(self, member, raw_body):
        def operation(conn, cipher, now):
            changes = store.validate_profile_changes(store.parse_json_object(raw_body))
            return _respond(200, store.update_profile(conn, cipher, member, changes, now))

        return self._run(operation)

    # -- entries -----------------------------------------------------------
    def entries_list(self, member, query):
        def operation(conn, cipher, now):
            limit = store.parse_limit(query.get("limit"))
            entries, next_cursor = store.list_entries(
                conn, cipher, member, limit, query.get("cursor")
            )
            return _respond(200, {"entries": entries, "limit": limit, "next_cursor": next_cursor})

        return self._run(operation)

    def entry_create(self, member, raw_body):
        def operation(conn, cipher, now):
            changes = store.validate_entry_changes(store.parse_json_object(raw_body))
            return _respond(201, store.create_entry(conn, cipher, member, changes, now))

        return self._run(operation)

    def entry_update(self, member, entry_id, raw_body):
        def operation(conn, cipher, now):
            changes = store.validate_entry_changes(store.parse_json_object(raw_body))
            return _respond(200, store.update_entry(conn, cipher, member, entry_id, changes, now))

        return self._run(operation)

    def entry_delete(self, member, entry_id):
        def operation(conn, cipher, now):
            store.delete_entry(conn, member, entry_id)
            return _respond(200, {"status": "deleted"})

        return self._run(operation)

    # -- whole-journal actions ---------------------------------------------
    def delete_all(self, member):
        def operation(conn, cipher, now):
            return _respond(200, {"status": "deleted", **store.delete_all(conn, member)})

        return self._run(operation)

    def export(self, member):
        def operation(conn, cipher, now):
            return _respond(
                200,
                store.export_all(conn, cipher, member, now),
                {"Content-Disposition": 'attachment; filename="filtersight-journal-export.json"'},
            )

        return self._run(operation)


# ---------------------------------------------------------------------------
# Retention cleanup lifecycle (asyncio only; no web framework needed)
# ---------------------------------------------------------------------------
async def _sweep_safely(get_db):
    try:
        await asyncio.to_thread(store.run_cleanup, get_db)
    except Exception as error:  # a failed sweep must never take the app down
        logger.error("journal.cleanup_failed error=%s", type(error).__name__)


async def _cleanup_loop(get_db, interval):
    while True:
        await asyncio.sleep(interval)
        await _sweep_safely(get_db)


async def start_cleanup(get_db, interval=CLEANUP_INTERVAL_SECONDS):
    """Sweep once right now (this is how a restart catches up on anything that
    came due while the service was down), then keep sweeping on a timer."""
    await _sweep_safely(get_db)
    return asyncio.create_task(_cleanup_loop(get_db, interval))


async def stop_cleanup(task):
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
