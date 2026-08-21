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
import stripe
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from twilio.rest import Client as TwilioClient
from encouragement_messages import ENCOURAGEMENT_MESSAGES
from chatbot import get_chat_response

app = FastAPI()

# --- Config (set these as real environment variables, never hardcode) -----
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")  # from Stripe dashboard
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")  # your Twilio number

twilio_client = TwilioClient(TWILIO_SID, TWILIO_AUTH_TOKEN) if TWILIO_SID else None

DB_PATH = "filtersight.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            email TEXT PRIMARY KEY,
            stripe_customer_id TEXT,
            active INTEGER DEFAULT 1,
            tier TEXT DEFAULT 'tier1',
            user_phone TEXT,
            accountability_phone TEXT,
            last_dns_seen TEXT,
            removal_fee_paid INTEGER DEFAULT 0
        )
    """)
    # Lightweight migration for DBs created before this update.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(customers)")}
    if "tier" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN tier TEXT DEFAULT 'tier1'")
    if "user_phone" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN user_phone TEXT")
    conn.commit()
    return conn


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
        email = session.get("customer_details", {}).get("email")
        customer_id = session.get("customer")
        tier = (session.get("metadata") or {}).get("tier", "tier1")
        if email:
            db.execute(
                """INSERT INTO customers (email, stripe_customer_id, active, tier)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(email) DO UPDATE SET
                     stripe_customer_id = excluded.stripe_customer_id,
                     active = 1,
                     tier = excluded.tier""",
                (email, customer_id, tier),
            )
            db.commit()

    elif event["type"] == "customer.subscription.deleted":
        customer_id = event["data"]["object"].get("customer")
        db.execute("UPDATE customers SET active = 0 WHERE stripe_customer_id = ?", (customer_id,))
        db.commit()

    db.close()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 2. Save a customer's phone number(s) for their tier.
# Called from the Streamlit app after the "Save phone number(s)" step.
# tier2 sends user_phone only; tier3 sends both. tier1 never calls this.
# ---------------------------------------------------------------------------
@app.post("/save-contact")
async def save_contact(email: str, tier: str = "tier1", user_phone: str = "", accountability_phone: str = ""):
    db = get_db()
    db.execute(
        """UPDATE customers
           SET tier = ?, user_phone = ?, accountability_phone = ?
           WHERE email = ?""",
        (tier, user_phone or None, accountability_phone or None, email),
    )
    db.commit()
    db.close()
    return {"status": "saved"}


# ---------------------------------------------------------------------------
# 3. Trigger a notification when a blocked-content attempt is detected.
#
# Tier-aware routing:
#   tier1 — no texts at all (filter-only, fully private)
#   tier2 — self-encouragement text to the user's own phone
#   tier3 — self-text to the user, PLUS a separate notification to the
#           accountability partner
#
# IMPORTANT — this is the piece that still needs wiring to a real signal:
# CleanBrowsing's free tier doesn't give you per-customer attempt logs.
# To detect actual attempts, you'll need either:
#   a) A paid DNS provider with a logging API you can poll (NextDNS's API
#      supports per-profile logs), or
#   b) Your own self-hosted resolver (e.g. AdGuard Home) where you control
#      and can query the block logs directly.
# This function is the last step in that chain — call it once you have a
# real "email X had a blocked attempt at time Y" signal from either source.
# ---------------------------------------------------------------------------
@app.post("/notify-attempt")
async def notify_attempt(email: str):
    if not twilio_client:
        raise HTTPException(status_code=500, detail="Twilio not configured")

    db = get_db()
    row = db.execute(
        "SELECT tier, user_phone, accountability_phone FROM customers WHERE email = ?", (email,)
    ).fetchone()
    db.close()

    if not row:
        return {"status": "no_customer_found"}

    tier, user_phone, accountability_phone = row
    notified = []

    # tier1: filter-only, no texts of any kind.
    if tier == "tier1":
        return {"status": "no_notifications_for_tier1"}

    # tier2 and tier3 both text the user themselves.
    if user_phone:
        self_message = random.choice(ENCOURAGEMENT_MESSAGES)
        twilio_client.messages.create(
            to=user_phone,
            from_=TWILIO_FROM_NUMBER,
            body=f"Filtersight: {self_message}",
        )
        notified.append("user")

    # Only tier3 also notifies the accountability partner.
    if tier == "tier3" and accountability_phone:
        twilio_client.messages.create(
            to=accountability_phone,
            from_=TWILIO_FROM_NUMBER,
            body=f"Filtersight: your accountability partner had a filter bypass attempt just now.",
        )
        notified.append("partner")

    return {"status": "notified", "recipients": notified}


# ---------------------------------------------------------------------------
# 4. In-the-moment support chat. The frontend calls this when someone opens
# the chat after a bypass attempt (in addition to, or instead of, texting
# their accountability contact — you decide the flow).
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    history: list = None


@app.post("/chat")
async def chat(body: ChatRequest):
    reply = get_chat_response(body.message, conversation_history=body.history)
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
import datetime

REMOVAL_SILENCE_HOURS = 8  # tune based on real usage patterns once you have data

@app.post("/record-dns-activity")
async def record_dns_activity(email: str):
    """Call this from a scheduled job that polls your DNS provider's log API."""
    db = get_db()
    db.execute(
        "UPDATE customers SET last_dns_seen = ? WHERE email = ?",
        (datetime.datetime.utcnow().isoformat(), email),
    )
    db.commit()
    db.close()
    return {"status": "recorded"}


@app.post("/check-for-removed-profiles")
async def check_for_removed_profiles():
    """Run this on a schedule (e.g. every hour) via cron or a scheduled task."""
    if not twilio_client:
        raise HTTPException(status_code=500, detail="Twilio not configured")

    db = get_db()
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=REMOVAL_SILENCE_HOURS)).isoformat()
    rows = db.execute(
        "SELECT email, tier, user_phone, accountability_phone FROM customers WHERE active = 1 AND last_dns_seen < ?",
        (cutoff,),
    ).fetchall()

    notified = []
    for email, tier, user_phone, accountability_phone in rows:
        if tier == "tier1":
            continue
        body = (
            f"Filtersight: it looks like the filter on {email}'s device may have been "
            f"removed or disabled — no activity in the last {REMOVAL_SILENCE_HOURS} hours."
        )
        if user_phone:
            twilio_client.messages.create(to=user_phone, from_=TWILIO_FROM_NUMBER, body=body)
        if tier == "tier3" and accountability_phone:
            twilio_client.messages.create(to=accountability_phone, from_=TWILIO_FROM_NUMBER, body=body)
        notified.append(email)
    db.close()
    return {"notified": notified}


# ---------------------------------------------------------------------------
# 6. Cancellation friction — the piece that's actually enforceable, since
# nothing stops someone from deleting the profile directly on their phone
# regardless of what the server thinks. What you CAN gate is the paid
# subscription itself: cancelling requires either the accountability
# contact being notified, or a small fee, instead of an instant free cancel.
# Only tier3 has a partner to notify — tier1/tier2 always go the fee route.
# ---------------------------------------------------------------------------
REMOVAL_FEE_CENTS = 500  # $5 — adjust as you like

@app.post("/request-cancellation")
async def request_cancellation(email: str, notify_contact_instead_of_paying: bool = True):
    db = get_db()
    row = db.execute(
        "SELECT tier, accountability_phone FROM customers WHERE email = ?", (email,)
    ).fetchone()

    tier, accountability_phone = row if row else (None, None)
    can_notify_partner = tier == "tier3" and accountability_phone

    if notify_contact_instead_of_paying and can_notify_partner and twilio_client:
        twilio_client.messages.create(
            to=accountability_phone,
            from_=TWILIO_FROM_NUMBER,
            body=f"Filtersight: {email} has requested to cancel their filter. "
                 f"Reaching out to check in is up to you.",
        )
        db.close()
        return {"status": "contact_notified", "next_step": "cancellation will proceed after notice"}

    # Fee path: charge a one-time fee via Stripe before actually cancelling.
    # This is also the only path for tier1/tier2, since they have no partner on file.
    if not stripe.api_key:
        db.close()
        raise HTTPException(status_code=500, detail="Stripe not configured")

    customer = db.execute(
        "SELECT stripe_customer_id FROM customers WHERE email = ?", (email,)
    ).fetchone()
    db.close()
    if not customer or not customer[0]:
        raise HTTPException(status_code=404, detail="Customer not found")

    checkout_session = stripe.checkout.Session.create(
        mode="payment",
        customer=customer[0],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Filtersight — cancellation processing fee"},
                "unit_amount": REMOVAL_FEE_CENTS,
            },
            "quantity": 1,
        }],
        success_url=f"{os.environ.get('APP_BASE_URL', '')}/?cancelled=true",
        cancel_url=os.environ.get("APP_BASE_URL", ""),
    )
    return {"status": "fee_required", "checkout_url": checkout_session.url}
