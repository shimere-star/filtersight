"""Filtersight backend for billing, member access, chat, and DNS profiles.

Run with: uvicorn webhook_server:app --host 0.0.0.0 --port 8000
"""

import os
import sqlite3
import datetime
import copy
import hmac
import uuid
import hashlib
import secrets
import logging
import time
import stripe
import requests
from contextlib import asynccontextmanager
from urllib.parse import urlencode
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
from chatbot import get_chat_response
import journal_router
import journal_service
import journal_store


@asynccontextmanager
async def lifespan(_app):
    # Private-journal retention: sweep once at startup (this catches anything that
    # came due while the service was down), then on a timer. All schedule state
    # lives in SQLite, so a restart loses nothing. See JOURNAL_BACKEND.md.
    cleanup_task = await journal_service.start_cleanup(lambda: get_db())
    try:
        yield
    finally:
        await journal_service.stop_cleanup(cleanup_task)


app = FastAPI(lifespan=lifespan)
logger = logging.getLogger(__name__)
# Railway captures stdout/stderr: make sure INFO logs are actually emitted.
logging.basicConfig(level=logging.INFO)

# --- Config (set these as real environment variables, never hardcode) -----
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")  # from Stripe dashboard
CURRENT_PLAN = "filtersight"
SUPPORTED_PLANS = {CURRENT_PLAN}

# --- NextDNS config (for real-time bypass detection) --------------------
NEXTDNS_API_KEY = os.environ.get("NEXTDNS_API_KEY")
NEXTDNS_PROFILE_ID = os.environ.get("NEXTDNS_PROFILE_ID")
BACKFILL_ADMIN_SECRET = os.environ.get("BACKFILL_ADMIN_SECRET")
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")
# Reuse the configured verified sender address; allow a SendGrid-specific name too.
SENDGRID_FROM_EMAIL = os.environ.get("SENDGRID_FROM_EMAIL") or os.environ.get("SMTP_FROM_EMAIL")

DB_PATH = "/data/customers.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            email TEXT PRIMARY KEY,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            active INTEGER DEFAULT 1,
            plan TEXT,
            nextdns_profile_id TEXT,
            removal_fee_paid INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS member_magic_links (
            email TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS member_sessions (
            token_hash TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            stripe_subscription_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    # Lightweight migration for DBs created before this update.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(customers)")}
    if "plan" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN plan TEXT")
    if "stripe_subscription_id" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN stripe_subscription_id TEXT")
    if "nextdns_profile_id" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN nextdns_profile_id TEXT")
    if "removal_fee_paid" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN removal_fee_paid INTEGER DEFAULT 0")

    legacy_text_columns = {
        "user_phone",
        "accountability_phone",
        "user_sms_consent_at",
        "user_sms_consent_version",
        "partner_opt_in_status",
        "partner_opt_in_confirmed_at",
    }
    legacy_flag_columns = {
        "user_sms_opted_in",
        "accountability_sms_opted_in",
    }
    assignments = [
        f"{column} = NULL" for column in sorted(existing_cols & legacy_text_columns)
    ] + [
        f"{column} = 0" for column in sorted(existing_cols & legacy_flag_columns)
    ]
    if assignments:
        conn.execute(f"UPDATE customers SET {', '.join(assignments)}")
    # Private journal tables: additive only, never alters the tables above.
    journal_store.ensure_schema(conn)
    conn.commit()
    return conn


def require_admin_secret(request: Request) -> None:
    supplied = request.headers.get("X-Admin-Secret", "")
    if not BACKFILL_ADMIN_SECRET:
        raise HTTPException(status_code=503, detail="Admin endpoints are not configured")
    if not hmac.compare_digest(supplied, BACKFILL_ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")


def verify_paid_checkout(checkout_session_id: str, allowed_plans=None):
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Stripe is not configured")
    if not checkout_session_id:
        raise HTTPException(status_code=400, detail="Checkout session is required")
    try:
        session = stripe.checkout.Session.retrieve(checkout_session_id)
    except stripe.error.StripeError:
        raise HTTPException(status_code=404, detail="Checkout session not found")

    if session.status != "complete" or session.payment_status not in ("paid", "no_payment_required"):
        raise HTTPException(status_code=402, detail="A completed paid checkout is required")
    subscription_id = session.subscription
    if not subscription_id:
        raise HTTPException(status_code=400, detail="Checkout has no subscription")
    try:
        subscription = stripe.Subscription.retrieve(subscription_id)
    except stripe.error.StripeError:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if subscription.status not in ("active", "trialing"):
        raise HTTPException(status_code=402, detail="Subscription is not active")

    metadata = session.metadata.to_dict() if session.metadata else {}
    plan = metadata.get("plan")
    if plan not in SUPPORTED_PLANS:
        raise HTTPException(status_code=403, detail="This plan is no longer available")
    if allowed_plans and plan not in allowed_plans:
        raise HTTPException(status_code=403, detail="This plan does not include this feature")
    details = session.customer_details
    email = (details.email if details and details.email else session.customer_email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Checkout has no customer email")
    return {"email": email, "plan": plan, "customer_id": session.customer, "subscription_id": subscription_id}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_utc_timestamp(value: str):
    if not value:
        return None
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _request_id() -> str:
    """Random correlation ID for logs — carries no information about the request."""
    return secrets.token_hex(6)


def send_member_signin_email(email: str, token: str) -> None:
    if not SENDGRID_API_KEY or not SENDGRID_FROM_EMAIL:
        raise HTTPException(status_code=503, detail="Member email sign-in is not configured")
    base_url = os.environ.get("APP_BASE_URL", "https://signup-app-v3-production.up.railway.app")
    link = f"{base_url}/?{urlencode({'view': 'member', 'magic_token': token})}"
    payload = {
        "personalizations": [{"to": [{"email": email}]}],
        "from": {"email": SENDGRID_FROM_EMAIL},
        "subject": "Your Filtersight sign-in link",
        "content": [{
            "type": "text/plain",
            "value": (
                "Use this one-time link to open your Filtersight member page. "
                "It expires in 15 minutes and can only be used once.\n\n"
                f"{link}\n\nIf you did not request this email, you can ignore it."
            ),
        }],
    }
    try:
        response = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
    except requests.RequestException as e:
        # Log only exception class; request details may contain private data.
        logger.error("send_member_signin_email request_failed error=%s", type(e).__name__)
        raise HTTPException(status_code=502, detail="Could not send the sign-in email") from e

    # SendGrid returns 202 when the message is accepted for processing.
    if response.status_code != 202:
        logger.error("send_member_signin_email rejected status=%s", response.status_code)
        raise HTTPException(status_code=502, detail="Could not send the sign-in email")


class MemberEmailRequest(BaseModel):
    email: str


class MemberMagicLinkRequest(BaseModel):
    token: str


@app.post("/member/request-link")
async def member_request_link(body: MemberEmailRequest):
    email = body.email.strip().lower()
    ref = _request_id()
    t0 = time.monotonic()
    logger.info("request-link received ref=%s", ref)
    if len(email) > 254 or "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        logger.info("request-link invalid_email ref=%s", ref)
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    if not SENDGRID_API_KEY or not SENDGRID_FROM_EMAIL:
        logger.error("request-link sendgrid_not_configured ref=%s", ref)
        raise HTTPException(status_code=503, detail="Member email sign-in is not configured")

    generic = "If that email has an active Filtersight subscription, a sign-in link will be sent."
    db = get_db()
    row = db.execute(
        """SELECT stripe_subscription_id FROM customers
           WHERE email = ? AND active = 1 AND stripe_subscription_id IS NOT NULL""",
        (email,),
    ).fetchone()
    logger.info("request-link db_lookup ref=%s matched=%s elapsed=%.1fs",
                ref, row is not None, time.monotonic() - t0)
    if not row:
        db.close()
        return {"status": generic}

    now = datetime.datetime.now(datetime.timezone.utc)
    previous = db.execute(
        "SELECT sent_at FROM member_magic_links WHERE email = ?", (email,)
    ).fetchone()
    if previous:
        try:
            last_sent = parse_utc_timestamp(previous[0])
            if last_sent and (now - last_sent).total_seconds() < 120:
                logger.info("request-link rate_limited ref=%s", ref)
                db.close()
                return {"status": generic}
        except ValueError:
            pass

    t1 = time.monotonic()
    try:
        subscription = stripe.Subscription.retrieve(row[0])
    except stripe.error.StripeError as e:
        logger.warning("request-link stripe_error ref=%s error=%s elapsed=%.1fs",
                       ref, type(e).__name__, time.monotonic() - t1)
        db.close()
        return {"status": generic}
    logger.info("request-link stripe_lookup ref=%s status=%s elapsed=%.1fs",
                ref, subscription.status, time.monotonic() - t1)
    if subscription.status not in ("active", "trialing"):
        logger.info("request-link subscription_inactive ref=%s", ref)
        db.close()
        return {"status": generic}

    token = secrets.token_urlsafe(32)
    sent_at = now.isoformat()
    expires_at = (now + datetime.timedelta(minutes=15)).isoformat()
    db.execute(
        """INSERT INTO member_magic_links (email, token_hash, expires_at, sent_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(email) DO UPDATE SET token_hash = excluded.token_hash,
               expires_at = excluded.expires_at, sent_at = excluded.sent_at""",
        (email, hash_token(token), expires_at, sent_at),
    )
    db.commit()
    db.close()
    t2 = time.monotonic()
    try:
        logger.info("request-link send_attempt ref=%s provider=sendgrid_https", ref)
        send_member_signin_email(email, token)
    except HTTPException as e:
        logger.error("request-link send_failed ref=%s status=%s elapsed=%.1fs",
                     ref, e.status_code, time.monotonic() - t2)
        db = get_db()
        db.execute("DELETE FROM member_magic_links WHERE email = ?", (email,))
        db.commit()
        db.close()
        return {"status": generic}
    logger.info("request-link send_accepted ref=%s status=202 elapsed=%.1fs",
                ref, time.monotonic() - t2)
    return {"status": generic}


@app.post("/member/verify-link")
async def member_verify_link(body: MemberMagicLinkRequest):
    if len(body.token) > 200:
        raise HTTPException(status_code=401, detail="Invalid or expired sign-in link")
    now = datetime.datetime.now(datetime.timezone.utc)
    db = get_db()
    row = db.execute(
        "SELECT email, expires_at FROM member_magic_links WHERE token_hash = ?",
        (hash_token(body.token),),
    ).fetchone()
    if not row:
        db.close()
        raise HTTPException(status_code=401, detail="Invalid or expired sign-in link")
    email, expires_at = row
    try:
        is_expired = parse_utc_timestamp(expires_at) <= now
    except (TypeError, ValueError):
        is_expired = True
    if is_expired:
        db.execute("UPDATE member_magic_links SET token_hash = '' WHERE email = ?", (email,))
        db.commit()
        db.close()
        raise HTTPException(status_code=401, detail="Invalid or expired sign-in link")

    account = db.execute(
        """SELECT stripe_subscription_id, plan FROM customers
           WHERE email = ? AND active = 1 AND stripe_subscription_id IS NOT NULL""",
        (email,),
    ).fetchone()
    if not account:
        db.close()
        raise HTTPException(status_code=401, detail="Subscription is not active")
    subscription_id, plan = account
    try:
        subscription = stripe.Subscription.retrieve(subscription_id)
    except stripe.error.StripeError:
        db.close()
        raise HTTPException(status_code=503, detail="Could not verify your subscription right now")
    if subscription.status not in ("active", "trialing"):
        db.close()
        raise HTTPException(status_code=401, detail="Subscription is not active")

    db.execute(
        "UPDATE member_magic_links SET token_hash = '' WHERE email = ?",
        (email,),
    )
    db.execute("DELETE FROM member_sessions WHERE expires_at <= ?", (now.isoformat(),))
    access_token = secrets.token_urlsafe(32)
    access_expires = now + datetime.timedelta(days=30)
    db.execute(
        """INSERT INTO member_sessions
           (token_hash, email, stripe_subscription_id, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (hash_token(access_token), email, subscription_id, access_expires.isoformat(), now.isoformat()),
    )
    db.commit()
    db.close()
    return {"access_token": access_token, "expires_at": access_expires.isoformat(), "plan": plan}


def require_member_session(request: Request):
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 200:
        raise HTTPException(status_code=401, detail="Sign in to continue")
    db = get_db()
    row = db.execute(
        """SELECT s.email, s.stripe_subscription_id, s.expires_at, c.plan
           FROM member_sessions s JOIN customers c
             ON c.stripe_subscription_id = s.stripe_subscription_id
           WHERE s.token_hash = ? AND c.active = 1""",
        (hash_token(token),),
    ).fetchone()
    db.close()
    if not row:
        raise HTTPException(status_code=401, detail="Sign in to continue")
    email, subscription_id, expires_at, plan = row
    if plan != CURRENT_PLAN:
        raise HTTPException(status_code=403, detail="This plan is no longer available")
    try:
        if parse_utc_timestamp(expires_at) <= datetime.datetime.now(datetime.timezone.utc):
            raise HTTPException(status_code=401, detail="Your sign-in expired. Request a new link.")
        subscription = stripe.Subscription.retrieve(subscription_id)
    except stripe.error.StripeError:
        raise HTTPException(status_code=503, detail="Could not verify your subscription right now")
    if subscription.status not in ("active", "trialing"):
        raise HTTPException(status_code=401, detail="Subscription is not active")
    return {"email": email, "subscription_id": subscription_id, "plan": plan, "subscription": subscription}


@app.get("/member/profile")
async def member_profile(request: Request):
    member = require_member_session(request)
    subscription = member["subscription"]
    item_list = getattr(getattr(subscription, "items", None), "data", None) or []
    current_period_end = item_list[0].current_period_end if item_list else None
    return {
        "email": member["email"],
        "plan": member["plan"],
        "has_chat": True,
        "cancel_at_period_end": bool(subscription.cancel_at_period_end),
        "current_period_end": current_period_end,
    }


class MemberChatRequest(BaseModel):
    message: str
    history: list = Field(default_factory=list)


@app.post("/member/chat")
async def member_chat(request: Request, body: MemberChatRequest):
    member = require_member_session(request)
    if len(body.message) > 4000:
        raise HTTPException(status_code=413, detail="Message is too long")
    safe_history = [
        item for item in body.history[-20:]
        if isinstance(item, dict)
        and item.get("role") in ("user", "assistant")
        and isinstance(item.get("content"), str)
    ]
    return {"reply": get_chat_response(body.message, conversation_history=safe_history)}


@app.post("/member/cancel")
async def member_cancel(request: Request):
    member = require_member_session(request)
    try:
        stripe.Subscription.modify(member["subscription_id"], cancel_at_period_end=True)
    except stripe.error.StripeError as e:
        logger.error(
            "member/cancel: Stripe %s (code=%s, request_id=%s, http_status=%s)",
            type(e).__name__,
            getattr(e, "code", None),
            getattr(e, "request_id", None),
            getattr(e, "http_status", None),
        )
        raise HTTPException(status_code=502, detail="Could not schedule cancellation right now")
    return {"status": "cancellation_scheduled", "cancel_at_period_end": True, "cancellation_fee": 0}


@app.post("/member/logout")
async def member_logout(request: Request):
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token and len(token) <= 200:
        db = get_db()
        db.execute("DELETE FROM member_sessions WHERE token_hash = ?", (hash_token(token),))
        db.commit()
        db.close()
    return {"status": "signed_out"}


# ---------------------------------------------------------------------------
# Private goal / journal / coping plan (see JOURNAL_BACKEND.md).
# Every route lives under /member/journal and is authenticated by the same
# member-session check as the other /member routes. Journal content is
# encrypted at rest and is never sent to an AI provider by the backend.
# ---------------------------------------------------------------------------
journal = journal_service.JournalService(lambda: get_db())


def _journal_member(request: Request):
    return require_member_session(request)


app.include_router(journal_router.build_router(journal, _journal_member))


def _journal_retention_hook(action, *args):
    """Run journal retention bookkeeping inside the caller's transaction.

    It must never be able to break billing or provisioning: failures are logged
    by class name only, and the periodic cleanup sweep reconciles anything missed.
    """
    try:
        return action(*args)
    except Exception as e:
        logger.error("journal retention hook failed: %s", type(e).__name__)
        return None


def create_nextdns_profile(customer_email: str, plan: str, subscription_id: str) -> str:
    if not NEXTDNS_API_KEY or not NEXTDNS_PROFILE_ID:
        raise HTTPException(status_code=503, detail="NextDNS profile template is not configured")
    headers = {"X-Api-Key": NEXTDNS_API_KEY}
    profile_name = f"Filtersight {hashlib.sha256(subscription_id.encode()).hexdigest()[:12]}"
    request_stage = "find_existing_profile"
    try:
        # Reuse a profile if an earlier POST completed but its response was lost.
        existing_response = requests.get(
            "https://api.nextdns.io/profiles",
            headers=headers,
            timeout=30,
        )
        if not existing_response.ok:
            logger.warning(
                "NextDNS profile list failed: status=%s response=%s",
                existing_response.status_code,
                existing_response.text[:500],
            )
        existing_response.raise_for_status()
        existing_data = existing_response.json().get("data", [])
        if isinstance(existing_data, list):
            existing = next(
                (item for item in existing_data if isinstance(item, dict) and item.get("name") == profile_name),
                None,
            )
            if existing and existing.get("id"):
                profile_id = existing["id"]
                request_stage = "disable_existing_profile_logs"
                updated = requests.patch(
                    f"https://api.nextdns.io/profiles/{profile_id}/settings/logs",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"enabled": False},
                    timeout=30,
                )
                updated.raise_for_status()
                try:
                    update_payload = updated.json()
                except ValueError:
                    update_payload = {}
                if isinstance(update_payload, dict) and update_payload.get("errors"):
                    raise HTTPException(
                        status_code=502,
                        detail="NextDNS rejected the privacy settings update",
                    )
                request_stage = "clear_existing_profile_logs"
                cleared = requests.delete(
                    f"https://api.nextdns.io/profiles/{profile_id}/logs",
                    headers=headers,
                    timeout=30,
                )
                cleared.raise_for_status()
                try:
                    clear_payload = cleared.json()
                except ValueError:
                    clear_payload = {}
                if isinstance(clear_payload, dict) and clear_payload.get("errors"):
                    raise HTTPException(
                        status_code=502,
                        detail="NextDNS rejected the stored-log deletion",
                    )
                return profile_id

        # Keep this payload deliberately small. The current plan does not need
        # browsing activity because members open the companion directly.
        profile = {
            "name": profile_name,
            "parentalControl": {
                "categories": [{"id": "porn", "active": True}],
            },
            "settings": {
                "logs": {
                    "enabled": False,
                    "drop": {"ip": True, "domain": False},
                },
            },
        }
        request_stage = "create_profile"
        created = requests.post(
            "https://api.nextdns.io/profiles",
            headers={**headers, "Content-Type": "application/json"},
            json=profile,
            timeout=60,
        )
        if not created.ok:
            logger.warning(
                "NextDNS profile creation failed: status=%s response=%s",
                created.status_code,
                created.text[:500],
            )
        created.raise_for_status()
        try:
            created_payload = created.json()
        except ValueError as e:
            logger.warning(
                "NextDNS profile creation returned invalid JSON: status=%s response=%s",
                created.status_code,
                created.text[:500],
            )
            raise HTTPException(status_code=502, detail="NextDNS returned an invalid profile response") from e
        profile_data = created_payload.get("data")
        profile_id = profile_data.get("id") if isinstance(profile_data, dict) else None
        if not profile_id:
            logger.warning(
                "NextDNS profile creation returned no profile ID: status=%s response=%s",
                created.status_code,
                created.text[:500],
            )
            raise HTTPException(status_code=502, detail="NextDNS did not return a profile ID")
        return profile_id
    except requests.RequestException as e:
        logger.warning(
            "NextDNS profile setup request failed: stage=%s error_type=%s",
            request_stage, type(e).__name__,
        )
        raise HTTPException(status_code=502, detail="NextDNS profile setup failed") from e


# ---------------------------------------------------------------------------
# 1. Stripe webhook — the source of truth for who's actually paid, and which
# plan they paid for (read from the checkout session metadata set in app.py).
# In your Stripe Dashboard, add an endpoint pointing to:
#   https://yourdomain.com/stripe-webhook
# and subscribe to: checkout.session.completed, customer.subscription.deleted
# ---------------------------------------------------------------------------
@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    db = get_db()
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_details = session.customer_details
        email = (
            customer_details.email.strip().lower()
            if customer_details and customer_details.email
            else None
        )
        customer_id = session.customer
        subscription_id = session.subscription
        metadata = session.metadata
        plan = metadata.to_dict().get("plan") if metadata else None
        if email and plan in SUPPORTED_PLANS:
            db.execute(
                """INSERT INTO customers (
                       email,
                       stripe_customer_id,
                       stripe_subscription_id,
                       active,
                       plan
                   )
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(email) DO UPDATE SET
                     stripe_customer_id = excluded.stripe_customer_id,
                     stripe_subscription_id = excluded.stripe_subscription_id,
                     active = 1,
                     plan = excluded.plan""",
                (email, customer_id, subscription_id, plan),
            )
            # Returning member: keep journal data whose deletion is still pending.
            _journal_retention_hook(journal_store.clear_pending_deletion, db, email)
            db.commit()

    elif event["type"] == "customer.subscription.deleted":
        ended_subscription = event["data"]["object"]
        customer_id = ended_subscription.customer
        db.execute("UPDATE customers SET active = 0 WHERE stripe_customer_id = ?", (customer_id,))
        # Journal data is deleted no later than 30 days after the subscription ends.
        _journal_retention_hook(
            journal_store.schedule_deletion_for_customer,
            db,
            customer_id,
            getattr(ended_subscription, "id", None),
            journal_store.timestamp_to_datetime(getattr(ended_subscription, "ended_at", None)),
        )
        db.commit()

    db.close()
    return {"status": "ok"}


class CheckoutSessionRequest(BaseModel):
    checkout_session_id: str


@app.post("/provision-nextdns-profile")
async def provision_nextdns_profile(body: CheckoutSessionRequest):
    """Create or reuse the customer's isolated NextDNS profile after verified payment."""
    checkout = verify_paid_checkout(body.checkout_session_id, SUPPORTED_PLANS)
    db = get_db()
    row = db.execute(
        "SELECT nextdns_profile_id FROM customers WHERE stripe_subscription_id = ?",
        (checkout["subscription_id"],),
    ).fetchone()
    if row and row[0]:
        db.close()
        return {"profile_id": row[0]}

    profile_id = create_nextdns_profile(checkout["email"], checkout["plan"], checkout["subscription_id"])
    db.execute(
        """INSERT INTO customers (
               email, stripe_customer_id, stripe_subscription_id, active, plan,
               nextdns_profile_id
           ) VALUES (?, ?, ?, 1, ?, ?)
           ON CONFLICT(email) DO UPDATE SET
               stripe_customer_id = excluded.stripe_customer_id,
               stripe_subscription_id = excluded.stripe_subscription_id,
               active = 1,
               plan = excluded.plan,
               nextdns_profile_id = COALESCE(customers.nextdns_profile_id, excluded.nextdns_profile_id)""",
        (checkout["email"], checkout["customer_id"], checkout["subscription_id"], checkout["plan"], profile_id),
    )
    # Returning member (this path also marks the account active): keep journal data
    # whose deletion is still pending; one that is already due is deleted now.
    _journal_retention_hook(journal_store.clear_pending_deletion, db, checkout["email"])
    db.commit()
    saved = db.execute(
        "SELECT nextdns_profile_id FROM customers WHERE stripe_subscription_id = ?",
        (checkout["subscription_id"],),
    ).fetchone()
    db.close()
    return {"profile_id": saved[0] if saved else profile_id}


class ChatRequest(BaseModel):
    message: str
    history: list = Field(default_factory=list)
    checkout_session_id: str


@app.post("/chat")
async def chat(body: ChatRequest):
    verify_paid_checkout(body.checkout_session_id, SUPPORTED_PLANS)
    if len(body.message) > 4000:
        raise HTTPException(status_code=413, detail="Message is too long")
    safe_history = [
        item for item in body.history[-20:]
        if isinstance(item, dict)
        and item.get("role") in ("user", "assistant")
        and isinstance(item.get("content"), str)
    ]
    reply = get_chat_response(body.message, conversation_history=safe_history)
    return {"reply": reply}


# ---------------------------------------------------------------------------
# 6. Cancellation — scheduled for the end of the current billing period.
# ---------------------------------------------------------------------------
@app.post("/request-cancellation")
async def request_cancellation(checkout_session_id: str):
    checkout = verify_paid_checkout(checkout_session_id, SUPPORTED_PLANS)
    subscription_id = checkout["subscription_id"]
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    try:
        stripe.Subscription.modify(
            subscription_id,
            cancel_at_period_end=True,
        )
    except stripe.error.StripeError as e:
        logger.error(
            "request-cancellation: Stripe %s (code=%s, request_id=%s, http_status=%s)",
            type(e).__name__,
            getattr(e, "code", None),
            getattr(e, "request_id", None),
            getattr(e, "http_status", None),
        )
        raise HTTPException(status_code=502, detail="Could not schedule cancellation right now")

    return {"status": "cancellation_scheduled", "cancel_at_period_end": True, "cancellation_fee": 0}
