"""
Filtersight backend — handles two jobs Streamlit can't do well on its own:

1. Stripe webhooks — confirms payments/cancellations server-side (never trust
   the frontend alone for this).
2. Accountability notifications — sends a text via Twilio when a customer's
   DNS filter logs a blocked-content attempt. Who gets texted depends on
   their tier: tier1 gets nothing (filter-only), tier2 gets a text to their
   own phone, tier3 gets that PLUS a text to their accountability partner.

Run with: uvicorn webhook_server:app --host 0.0.0.0 --port 8000
"""

import os
import random
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
import phonenumbers
from phonenumbers import NumberParseException
from urllib.parse import parse_qs, urlencode
from fastapi import FastAPI, Request, HTTPException, Response
from pydantic import BaseModel, Field
from twilio.rest import Client as TwilioClient
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
from encouragement_messages import ENCOURAGEMENT_MESSAGES
from chatbot import get_chat_response

app = FastAPI()
logger = logging.getLogger(__name__)
# Railway captures stdout/stderr: make sure INFO logs are actually emitted.
logging.basicConfig(level=logging.INFO)

# --- Config (set these as real environment variables, never hardcode) -----
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")  # from Stripe dashboard
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")  # your Twilio number
SMS_CONSENT_VERSION = "2026-09-25-v1"
ENABLE_TIER2_TIER3 = os.environ.get("ENABLE_TIER2_TIER3", "false").lower() in ("1", "true", "yes")
ENABLE_SMS = os.environ.get("ENABLE_SMS", "false").lower() in ("1", "true", "yes")

twilio_client = TwilioClient(TWILIO_SID, TWILIO_AUTH_TOKEN) if TWILIO_SID else None

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
            tier TEXT DEFAULT 'tier1',
            user_phone TEXT,
            partner_opt_in_status TEXT,
            partner_opt_in_confirmed_at TEXT,
            accountability_phone TEXT,
            user_sms_opted_in INTEGER DEFAULT 0,
            user_sms_consent_at TEXT,
            user_sms_consent_version TEXT,
            accountability_sms_opted_in INTEGER DEFAULT 0,
            nextdns_profile_id TEXT,
            nextdns_last_checked_at TEXT,
            nextdns_pagination_cursor TEXT,
            last_dns_seen TEXT,
            last_removed_alert_at TEXT,
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
    if "tier" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN tier TEXT DEFAULT 'tier1'")
    if "user_phone" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN user_phone TEXT")
    if "accountability_phone" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN accountability_phone TEXT")
    if "user_sms_opted_in" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN user_sms_opted_in INTEGER DEFAULT 0")
        # Preserve legacy customers: under the old flow, having user_phone
        # meant the customer had already supplied a texting number.
        conn.execute("UPDATE customers SET user_sms_opted_in = 1 WHERE user_phone IS NOT NULL AND TRIM(user_phone) != ''")
    if "user_sms_consent_at" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN user_sms_consent_at TEXT")
    if "user_sms_consent_version" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN user_sms_consent_version TEXT")
    if "accountability_sms_opted_in" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN accountability_sms_opted_in INTEGER DEFAULT 0")
    if "partner_opt_in_status" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN partner_opt_in_status TEXT")
    if "partner_opt_in_confirmed_at" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN partner_opt_in_confirmed_at TEXT")
    if "stripe_subscription_id" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN stripe_subscription_id TEXT")
    if "nextdns_profile_id" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN nextdns_profile_id TEXT")
    if "nextdns_last_checked_at" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN nextdns_last_checked_at TEXT")
    if "nextdns_pagination_cursor" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN nextdns_pagination_cursor TEXT")
    if "last_dns_seen" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN last_dns_seen TEXT")
    if "last_removed_alert_at" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN last_removed_alert_at TEXT")
    if "removal_fee_paid" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN removal_fee_paid INTEGER DEFAULT 0")
    conn.commit()
    return conn


def require_admin_secret(request: Request) -> None:
    supplied = request.headers.get("X-Admin-Secret", "")
    if not BACKFILL_ADMIN_SECRET:
        raise HTTPException(status_code=503, detail="Admin endpoints are not configured")
    if not hmac.compare_digest(supplied, BACKFILL_ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")


def verify_paid_checkout(checkout_session_id: str, allowed_tiers=None):
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
    tier = metadata.get("tier", "tier1")
    if tier in ("tier2", "tier3") and not ENABLE_TIER2_TIER3:
        raise HTTPException(status_code=503, detail="Tier 2 and Tier 3 are not enabled yet")
    if allowed_tiers and tier not in allowed_tiers:
        raise HTTPException(status_code=403, detail="This plan does not include this feature")
    details = session.customer_details
    email = (details.email if details and details.email else session.customer_email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Checkout has no customer email")
    return {"email": email, "tier": tier, "customer_id": session.customer, "subscription_id": subscription_id}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
        """SELECT stripe_subscription_id, tier FROM customers
           WHERE email = ? AND active = 1 AND stripe_subscription_id IS NOT NULL""",
        (email,),
    ).fetchone()
    if not account:
        db.close()
        raise HTTPException(status_code=401, detail="Subscription is not active")
    subscription_id, tier = account
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
    return {"access_token": access_token, "expires_at": access_expires.isoformat(), "tier": tier}


def require_member_session(request: Request):
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 200:
        raise HTTPException(status_code=401, detail="Sign in to continue")
    db = get_db()
    row = db.execute(
        """SELECT s.email, s.stripe_subscription_id, s.expires_at, c.tier
           FROM member_sessions s JOIN customers c
             ON c.stripe_subscription_id = s.stripe_subscription_id
           WHERE s.token_hash = ? AND c.active = 1""",
        (hash_token(token),),
    ).fetchone()
    db.close()
    if not row:
        raise HTTPException(status_code=401, detail="Sign in to continue")
    email, subscription_id, expires_at, tier = row
    try:
        if parse_utc_timestamp(expires_at) <= datetime.datetime.now(datetime.timezone.utc):
            raise HTTPException(status_code=401, detail="Your sign-in expired. Request a new link.")
        subscription = stripe.Subscription.retrieve(subscription_id)
    except stripe.error.StripeError:
        raise HTTPException(status_code=503, detail="Could not verify your subscription right now")
    if subscription.status not in ("active", "trialing"):
        raise HTTPException(status_code=401, detail="Subscription is not active")
    return {"email": email, "subscription_id": subscription_id, "tier": tier, "subscription": subscription}


@app.get("/member/profile")
async def member_profile(request: Request):
    member = require_member_session(request)
    return {
        "email": member["email"],
        "tier": member["tier"],
        "has_chat": ENABLE_TIER2_TIER3 and member["tier"] in ("tier2", "tier3"),
        "cancel_at_period_end": bool(member["subscription"].cancel_at_period_end),
        "current_period_end": member["subscription"].current_period_end,
    }


class MemberChatRequest(BaseModel):
    message: str
    history: list = Field(default_factory=list)


@app.post("/member/chat")
async def member_chat(request: Request, body: MemberChatRequest):
    member = require_member_session(request)
    if not ENABLE_TIER2_TIER3 or member["tier"] not in ("tier2", "tier3"):
        raise HTTPException(status_code=403, detail="The companion is available on Tier 2 and Tier 3")
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
    except stripe.error.StripeError:
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


def create_nextdns_profile(customer_email: str, tier: str, subscription_id: str) -> str:
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
                return existing["id"]

        # Keep this payload deliberately small while isolating the API failure:
        # adult-content filtering plus only the log controls needed by each tier.
        profile = {
            "name": profile_name,
            "parentalControl": {
                "categories": [{"id": "porn", "active": True}],
            },
            "settings": {
                "logs": {
                    "enabled": tier in ("tier2", "tier3"),
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


def send_attempt_notifications(row, domain: str = ""):
    tier, user_phone, accountability_phone, user_opted_in, partner_opted_in, partner_status = row
    if tier == "tier1" or not ENABLE_TIER2_TIER3 or not ENABLE_SMS:
        return []
    send_to_user = bool(user_phone and user_opted_in)
    send_to_partner = bool(
        tier == "tier3" and partner_status == "confirmed"
        and accountability_phone and partner_opted_in
    )
    if not send_to_user and not send_to_partner:
        return []
    if not twilio_client:
        raise HTTPException(status_code=503, detail="Twilio is not configured")

    notified = []
    if send_to_user:
        message = random.choice(ENCOURAGEMENT_MESSAGES)
        twilio_client.messages.create(
            to=user_phone,
            from_=TWILIO_FROM_NUMBER,
            body=f"Filtersight: {message} Reply STOP to opt out.",
        )
        notified.append("user")
    if send_to_partner:
        twilio_client.messages.create(
            to=accountability_phone,
            from_=TWILIO_FROM_NUMBER,
            body="Filtersight: your accountability partner had a filter bypass attempt just now. Reply STOP to opt out.",
        )
        notified.append("partner")
    return notified


# ---------------------------------------------------------------------------
# 1. Stripe webhook — the source of truth for who's actually paid, and which
# tier they paid for (read from the checkout session metadata set in app.py).
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
        tier = metadata.to_dict().get("tier", "tier1") if metadata else "tier1"
        if email:
            db.execute(
                """INSERT INTO customers (
                       email,
                       stripe_customer_id,
                       stripe_subscription_id,
                       active,
                       tier
                   )
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(email) DO UPDATE SET
                     stripe_customer_id = excluded.stripe_customer_id,
                     stripe_subscription_id = excluded.stripe_subscription_id,
                     active = 1,
                     tier = excluded.tier""",
                (email, customer_id, subscription_id, tier),
            )
            db.commit()

    elif event["type"] == "customer.subscription.deleted":
        customer_id = event["data"]["object"].customer
        db.execute("UPDATE customers SET active = 0 WHERE stripe_customer_id = ?", (customer_id,))
        db.commit()

    db.close()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
def normalize_phone_e164(phone: str) -> str:
    phone = (phone or "").strip()
    if not phone:
        return ""

    try:
        parsed = phonenumbers.parse(phone, "US")
    except NumberParseException:
        raise HTTPException(status_code=400, detail="Invalid phone number")

    if not phonenumbers.is_valid_number(parsed):
        raise HTTPException(status_code=400, detail="Invalid phone number")

    return phonenumbers.format_number(
        parsed,
        phonenumbers.PhoneNumberFormat.E164,
    )


class CheckoutSessionRequest(BaseModel):
    checkout_session_id: str


@app.post("/provision-nextdns-profile")
async def provision_nextdns_profile(body: CheckoutSessionRequest):
    """Create or reuse the customer's isolated NextDNS profile after verified payment."""
    checkout = verify_paid_checkout(body.checkout_session_id, {"tier1", "tier2", "tier3"})
    db = get_db()
    row = db.execute(
        "SELECT nextdns_profile_id FROM customers WHERE stripe_subscription_id = ?",
        (checkout["subscription_id"],),
    ).fetchone()
    if row and row[0]:
        db.close()
        return {"profile_id": row[0]}

    profile_id = create_nextdns_profile(checkout["email"], checkout["tier"], checkout["subscription_id"])
    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    db.execute(
        """INSERT INTO customers (
               email, stripe_customer_id, stripe_subscription_id, active, tier,
               nextdns_profile_id, nextdns_last_checked_at, last_dns_seen
           ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)
           ON CONFLICT(email) DO UPDATE SET
               stripe_customer_id = excluded.stripe_customer_id,
               stripe_subscription_id = excluded.stripe_subscription_id,
               active = 1,
               tier = excluded.tier,
               nextdns_profile_id = COALESCE(customers.nextdns_profile_id, excluded.nextdns_profile_id),
               nextdns_last_checked_at = COALESCE(customers.nextdns_last_checked_at, excluded.nextdns_last_checked_at),
               last_dns_seen = COALESCE(customers.last_dns_seen, excluded.last_dns_seen)""",
        (checkout["email"], checkout["customer_id"], checkout["subscription_id"], checkout["tier"], profile_id, checked_at, checked_at),
    )
    db.commit()
    saved = db.execute(
        "SELECT nextdns_profile_id FROM customers WHERE stripe_subscription_id = ?",
        (checkout["subscription_id"],),
    ).fetchone()
    db.close()
    return {"profile_id": saved[0] if saved else profile_id}


class SaveContactRequest(BaseModel):
    checkout_session_id: str
    user_phone: str = ""
    accountability_phone: str = ""
    user_sms_opted_in: bool = False

# 2. Save a customer's phone number(s) for their tier.
# Called from the Streamlit app after the "Save phone number(s)" step.
# tier2 sends user_phone only; tier3 sends both. tier1 never calls this.
# ---------------------------------------------------------------------------
@app.post("/save-contact")
async def save_contact(body: SaveContactRequest):
    checkout = verify_paid_checkout(body.checkout_session_id, {"tier2", "tier3"})
    tier = checkout["tier"]
    user_phone = normalize_phone_e164(body.user_phone)
    accountability_phone = normalize_phone_e164(body.accountability_phone)
    if body.user_sms_opted_in and not user_phone:
        raise HTTPException(status_code=400, detail="A phone number is required when opting in to SMS")
    if tier == "tier3" and not accountability_phone:
        raise HTTPException(status_code=400, detail="An accountability partner phone number is required")
    if accountability_phone and ENABLE_SMS and not twilio_client:
        raise HTTPException(status_code=503, detail="SMS invitations are not configured")

    db = get_db()
    previous = db.execute(
        """SELECT accountability_phone, partner_opt_in_status,
                  partner_opt_in_confirmed_at, accountability_sms_opted_in
           FROM customers WHERE stripe_subscription_id = ?""",
        (checkout["subscription_id"],),
    ).fetchone()
    if not previous:
        db.close()
        raise HTTPException(status_code=404, detail="Subscription record not found; retry after payment sync")
    invitation_needed = bool(accountability_phone) and (
        previous[0] != accountability_phone or previous[1] not in ("pending", "confirmed")
    )
    send_invitation = invitation_needed and ENABLE_SMS
    db.execute(
        """UPDATE customers
           SET tier = ?, user_phone = ?, accountability_phone = ?, user_sms_opted_in = ?,
               user_sms_consent_at = CASE WHEN ? = 1 THEN ? ELSE user_sms_consent_at END,
               user_sms_consent_version = CASE WHEN ? = 1 THEN ? ELSE user_sms_consent_version END,
               partner_opt_in_status = ?, partner_opt_in_confirmed_at = ?,
               accountability_sms_opted_in = ?
           WHERE stripe_subscription_id = ?""",
        (tier, user_phone or None, accountability_phone or None,
         int(body.user_sms_opted_in), int(body.user_sms_opted_in),
         datetime.datetime.now(datetime.timezone.utc).isoformat(),
         int(body.user_sms_opted_in), SMS_CONSENT_VERSION,
         "pending" if accountability_phone and send_invitation else (
             "not_invited" if accountability_phone and invitation_needed else (
             previous[1] if accountability_phone else None
             )
         ),
         previous[2] if accountability_phone and not invitation_needed else None,
         previous[3] if accountability_phone and not invitation_needed else 0,
         checkout["subscription_id"]),
    )
    db.commit()

    if send_invitation:
        try:
            twilio_client.messages.create(
                body=(
                    "Filtersight: someone added this number as an accountability partner "
                    "to receive filter bypass alerts. Reply YES to opt in, or STOP to decline. "
                    "Msg & data rates may apply."
                ),
                from_=TWILIO_FROM_NUMBER,
                to=accountability_phone,
            )
        except Exception as e:
            db.close()
            raise HTTPException(status_code=502, detail=f"Could not send the partner opt-in invitation: {e}")
    db.close()
    current_status = (
        "pending" if send_invitation else
        "not_invited" if invitation_needed else
        (previous[1] if accountability_phone else None)
    )
    return {"status": "saved", "partner_opt_in_status": current_status, "sms_enabled": ENABLE_SMS}


# ---------------------------------------------------------------------------
# 2b. Twilio inbound SMS webhook.
# Twilio sends form-encoded fields including From and Body.
# SMS opt-in state is tracked separately for the user's phone and the
# accountability partner's phone so STOP from one recipient does not
# accidentally unsubscribe the other recipient or cancel the paid plan.
# ---------------------------------------------------------------------------
OPT_IN_KEYWORDS = {"START", "YES", "UNSTOP"}
OPT_OUT_KEYWORDS = {
    "CANCEL",
    "QUIT",
    "STOP",
    "OPTOUT",
    "UNSUBSCRIBE",
    "STOPALL",
    "REVOKE",
    "END",
}
HELP_KEYWORDS = {"HELP", "INFO"}

OPT_IN_MESSAGE = "Filtersight: You are now opted-in. For help, reply HELP. To opt-out, reply STOP."
OPT_OUT_MESSAGE = "You have successfully been unsubscribed. You will not receive any more messages from this number. Reply START to resubscribe."
HELP_MESSAGE = (
    "Filtersight support: Reply STOP to unsubscribe. "
    "For help, contact support@filtersight.com. "
    "Msg & data rates may apply."
)


def twiml_response(message: str) -> Response:
    response = MessagingResponse()
    if message:
        response.message(message)
    return Response(content=str(response), media_type="application/xml")


@app.post("/sms-webhook")
async def sms_webhook(request: Request):
    payload = await request.body()
    form = parse_qs(payload.decode("utf-8"), keep_blank_values=True)
    if not TWILIO_AUTH_TOKEN:
        raise HTTPException(status_code=503, detail="Twilio webhook validation is not configured")
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    validator_params = {key: values[-1] for key, values in form.items() if values}
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validator.validate(str(request.url), validator_params, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    from_number = form.get("From", [""])[0].strip()
    message_body = form.get("Body", [""])[0].strip().upper()

    if from_number:
        try:
            from_number = normalize_phone_e164(from_number)
        except HTTPException:
            return twiml_response("")

    db = get_db()
    try:
        row = db.execute(
            """
            SELECT email, user_phone, accountability_phone,
                   user_sms_opted_in, accountability_sms_opted_in,
                   partner_opt_in_status
            FROM customers
            WHERE user_phone = ? OR accountability_phone = ?
            LIMIT 1
            """,
            (from_number, from_number),
        ).fetchone()

        if message_body in OPT_OUT_KEYWORDS:
            if row:
                (
                    email,
                    user_phone,
                    accountability_phone,
                    _,
                    _,
                    partner_opt_in_status,
                ) = row
                email = email.strip().lower()

                if from_number == accountability_phone:
                    db.execute(
                        """
                        UPDATE customers
                        SET accountability_sms_opted_in = 0,
                            partner_opt_in_status = 'declined',
                            partner_opt_in_confirmed_at = NULL
                        WHERE email = ?
                        """,
                        (email,),
                    )
                elif from_number == user_phone:
                    db.execute(
                        "UPDATE customers SET user_sms_opted_in = 0 WHERE email = ?",
                        (email,),
                    )

                db.commit()

            return twiml_response(OPT_OUT_MESSAGE)

        if message_body in OPT_IN_KEYWORDS:
            if row:
                (
                    email,
                    user_phone,
                    accountability_phone,
                    _,
                    _,
                    partner_opt_in_status,
                ) = row
                email = email.strip().lower()

                if (
                    from_number == accountability_phone
                    and partner_opt_in_status == "pending"
                ):
                    db.execute(
                        """
                        UPDATE customers
                        SET accountability_sms_opted_in = 1,
                            partner_opt_in_status = 'confirmed',
                            partner_opt_in_confirmed_at = ?
                        WHERE email = ?
                        """,
                        (datetime.datetime.utcnow().isoformat(), email),
                    )
                    db.commit()

                    return twiml_response(
                        "Filtersight: You are now confirmed as an accountability "
                        "partner and will receive alerts if a bypass attempt is detected. "
                        "Reply STOP to opt out."
                    )

                elif from_number == user_phone:
                    db.execute(
                        "UPDATE customers SET user_sms_opted_in = 1 WHERE email = ?",
                        (email,),
                    )
                    db.commit()

            return twiml_response(OPT_IN_MESSAGE)

        if message_body in HELP_KEYWORDS:
            return twiml_response(HELP_MESSAGE)

        return twiml_response("")

    finally:
        db.close()



# ---------------------------------------------------------------------------
# 3. Trigger a notification when a blocked-content attempt is detected.
#
# Tier-aware routing:
#   tier1 — no texts at all (filter-only, fully private)
#   tier2 — self-encouragement text to the user's own phone
#   tier3 — self-text to the user, PLUS a separate notification to the
#           accountability partner
#
# The protected NextDNS polling endpoint below supplies the real per-profile
# signal used by this route. A scheduled caller must invoke it regularly.
# ---------------------------------------------------------------------------
@app.post("/notify-attempt")
async def notify_attempt(request: Request, email: str):
    require_admin_secret(request)
    email = email.strip().lower()
    db = get_db()
    row = db.execute(
        """SELECT tier, user_phone, accountability_phone,
                  user_sms_opted_in, accountability_sms_opted_in,
                  partner_opt_in_status
           FROM customers WHERE email = ?""",
        (email,),
    ).fetchone()
    db.close()

    if not row:
        return {"status": "no_customer_found"}

    if row[0] == "tier1":
        return {"status": "no_notifications_for_tier1"}
    return {"status": "notified", "recipients": send_attempt_notifications(row)}


# ---------------------------------------------------------------------------
# 3b. Poll each active Tier 2/3 customer's isolated NextDNS profile.
# Protect this scheduler endpoint with BACKFILL_ADMIN_SECRET.
# ---------------------------------------------------------------------------
@app.post("/poll-nextdns-and-notify")
async def poll_nextdns_and_notify(request: Request):
    require_admin_secret(request)
    if not NEXTDNS_API_KEY:
        raise HTTPException(status_code=503, detail="NextDNS is not configured")

    db = get_db()
    customers = db.execute(
        """SELECT email, tier, nextdns_profile_id, nextdns_last_checked_at,
                  nextdns_pagination_cursor, user_phone, accountability_phone,
                  user_sms_opted_in, accountability_sms_opted_in, partner_opt_in_status
           FROM customers
           WHERE active = 1 AND tier IN ('tier2', 'tier3')
             AND nextdns_profile_id IS NOT NULL"""
    ).fetchall()
    notifications_sent = 0
    profiles_polled = 0
    failures = []
    headers = {"X-Api-Key": NEXTDNS_API_KEY}

    for customer in customers:
        (email, tier, profile_id, last_checked_at, saved_cursor, user_phone,
         accountability_phone, user_opted_in, partner_opted_in, partner_status) = customer
        params = {"limit": 1000, "sort": "asc"}
        if saved_cursor:
            params["cursor"] = saved_cursor
        elif last_checked_at:
            params["from"] = last_checked_at

        next_cursor = None
        latest_timestamp = last_checked_at
        pages = 0
        try:
            while True:
                if next_cursor:
                    params["cursor"] = next_cursor
                response = requests.get(
                    f"https://api.nextdns.io/profiles/{profile_id}/logs",
                    headers=headers,
                    params=params,
                    timeout=20,
                )
                response.raise_for_status()
                payload = response.json()
                for entry in payload.get("data", []):
                    entry_time = entry.get("timestamp")
                    if not entry_time or (last_checked_at and entry_time <= last_checked_at):
                        continue
                    if latest_timestamp is None or entry_time > latest_timestamp:
                        latest_timestamp = entry_time
                    db.execute(
                        "UPDATE customers SET last_dns_seen = ?, last_removed_alert_at = NULL WHERE email = ?",
                        (entry_time, email),
                    )
                    is_porn_block = entry.get("status") == "blocked" and any(
                        "porn" in (reason.get("id") or "").lower()
                        for reason in entry.get("reasons", [])
                    )
                    if is_porn_block:
                        recipients = send_attempt_notifications(
                            (tier, user_phone, accountability_phone, user_opted_in,
                             partner_opted_in, partner_status)
                        )
                        notifications_sent += len(recipients)

                next_cursor = payload.get("meta", {}).get("pagination", {}).get("cursor")
                pages += 1
                if not next_cursor:
                    break
                if pages >= 100:
                    break

            db.execute(
                """UPDATE customers
                   SET nextdns_last_checked_at = COALESCE(?, nextdns_last_checked_at),
                       nextdns_pagination_cursor = ?
                   WHERE email = ?""",
                (latest_timestamp if not next_cursor else None, next_cursor, email),
            )
            db.commit()
            profiles_polled += 1
        except (requests.RequestException, ValueError) as e:
            db.rollback()
            failures.append(profile_id)

    db.close()
    return {
        "status": "polled",
        "profiles_polled": profiles_polled,
        "notifications_sent": notifications_sent,
        "failed_profiles": failures,
    }


# ---------------------------------------------------------------------------
# 4. In-the-moment support chat. The frontend calls this when someone opens
# the chat after a bypass attempt (in addition to, or instead of, texting
# their accountability contact — you decide the flow).
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    history: list = Field(default_factory=list)
    checkout_session_id: str


@app.post("/chat")
async def chat(body: ChatRequest):
    verify_paid_checkout(body.checkout_session_id, {"tier2", "tier3"})
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
# 5. Removal detection via DNS "heartbeat" — the honest version.
#
# LIMITATION: iOS doesn't notify a third-party server when someone deletes a
# configuration profile — that level of control needs real MDM enrollment,
# which is a much bigger ask for a personal device and isn't the right fit
# here. This heartbeat approach is the practical alternative: your DNS
# provider (NextDNS, or your own AdGuard Home) logs every query. Call
# record_dns_activity() from a scheduled job that polls those logs. Then
# check_for_removed_profiles() looks for anyone who's gone quiet.
#
# Removal alerts go to whichever number(s) the tier actually has on file —
# tier1 has none, so nothing fires for them.
# ---------------------------------------------------------------------------

REMOVAL_SILENCE_HOURS = 8  # tune based on real usage patterns once you have data
REMOVAL_ALERT_COOLDOWN_HOURS = 24


def parse_utc_timestamp(value: str):
    if not value:
        return None
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=datetime.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(datetime.timezone.utc)

@app.post("/record-dns-activity")
async def record_dns_activity(request: Request, email: str):
    """Call this from a scheduled job that polls your DNS provider's log API."""
    require_admin_secret(request)
    email = email.strip().lower()
    db = get_db()
    db.execute(
        "UPDATE customers SET last_dns_seen = ? WHERE email = ?",
        (datetime.datetime.utcnow().isoformat(), email),
    )
    db.commit()
    db.close()
    return {"status": "recorded"}


@app.post("/check-for-removed-profiles")
async def check_for_removed_profiles(request: Request):
    """Run this on a schedule (e.g. every hour) via cron or a scheduled task."""
    require_admin_secret(request)
    if not ENABLE_TIER2_TIER3:
        raise HTTPException(status_code=503, detail="Tier 2 and Tier 3 are not enabled yet")
    if not ENABLE_SMS:
        return {"notified": [], "sms_enabled": False}
    if not twilio_client:
        raise HTTPException(status_code=500, detail="Twilio not configured")

    db = get_db()
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = db.execute(
        """SELECT email, tier, user_phone, accountability_phone,
                  user_sms_opted_in, accountability_sms_opted_in,
                  partner_opt_in_status, last_dns_seen, last_removed_alert_at
           FROM customers WHERE active = 1 AND tier IN ('tier2', 'tier3')
             AND last_dns_seen IS NOT NULL""",
    ).fetchall()

    notified = []
    cutoff = now - datetime.timedelta(hours=REMOVAL_SILENCE_HOURS)
    alert_cutoff = now - datetime.timedelta(hours=REMOVAL_ALERT_COOLDOWN_HOURS)
    for (email, tier, user_phone, accountability_phone, user_sms_opted_in,
         accountability_sms_opted_in, partner_opt_in_status, last_dns_seen,
         last_removed_alert_at) in rows:
        try:
            last_seen = parse_utc_timestamp(last_dns_seen)
            last_alert = parse_utc_timestamp(last_removed_alert_at)
        except ValueError:
            continue
        if not last_seen or last_seen >= cutoff or (last_alert and last_alert >= alert_cutoff):
            continue
        body = (
            f"Filtersight: it looks like the filter on {email}'s device may have been "
            f"removed or disabled — no activity in the last {REMOVAL_SILENCE_HOURS} hours."
        )
        if ENABLE_SMS and user_phone and user_sms_opted_in:
            twilio_client.messages.create(to=user_phone, from_=TWILIO_FROM_NUMBER, body=f"{body} Reply STOP to opt out.")
        if (
            ENABLE_SMS
            and
            tier == "tier3"
            and partner_opt_in_status == "confirmed"
            and accountability_phone
            and accountability_sms_opted_in
        ):
            twilio_client.messages.create(to=accountability_phone, from_=TWILIO_FROM_NUMBER, body=f"{body} Reply STOP to opt out.")
        db.execute(
            "UPDATE customers SET last_removed_alert_at = ? WHERE email = ?",
            (now.isoformat(), email),
        )
        notified.append(email)
    db.commit()
    db.close()
    return {"notified": notified}


# ---------------------------------------------------------------------------
# 6. Cancellation — scheduled for the end of the current billing period.
# ---------------------------------------------------------------------------
@app.post("/request-cancellation")
async def request_cancellation(checkout_session_id: str):
    checkout = verify_paid_checkout(checkout_session_id, {"tier1", "tier2", "tier3"})
    subscription_id = checkout["subscription_id"]
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    try:
        stripe.Subscription.modify(
            subscription_id,
            cancel_at_period_end=True,
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe cancellation failed: {e}")

    return {"status": "cancellation_scheduled", "cancel_at_period_end": True, "cancellation_fee": 0}
