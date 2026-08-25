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
import stripe
import requests
from urllib.parse import parse_qs
from fastapi import FastAPI, Request, HTTPException, Response
from pydantic import BaseModel
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
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

# --- NextDNS config (for real-time bypass detection) --------------------
NEXTDNS_API_KEY = os.environ.get("NEXTDNS_API_KEY")
NEXTDNS_PROFILE_ID = os.environ.get("NEXTDNS_PROFILE_ID")
# TEMPORARY: single-profile testing only. Real multi-customer support needs
# a NextDNS profile created per customer at signup, with the profile ID
# stored on their row instead of one shared TEST_CUSTOMER_EMAIL.
TEST_CUSTOMER_EMAIL = os.environ.get("TEST_CUSTOMER_EMAIL")

DB_PATH = "/data/customers.db"


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
            user_sms_opted_in INTEGER DEFAULT 1,
            accountability_sms_opted_in INTEGER DEFAULT 1,
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
    if "user_sms_opted_in" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN user_sms_opted_in INTEGER DEFAULT 1")
    if "accountability_sms_opted_in" not in existing_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN accountability_sms_opted_in INTEGER DEFAULT 1")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nextdns_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_checked_at TEXT
        )
    """)
    conn.execute(
        "INSERT OR IGNORE INTO nextdns_state (id, last_checked_at) VALUES (1, ?)",
        (datetime.datetime.utcnow().isoformat(),),
    )
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
        customer_details = session.customer_details
        email = (
            customer_details.email.strip().lower()
            if customer_details and customer_details.email
            else None
        )
        customer_id = session.customer
        metadata = session.metadata
        tier = metadata.to_dict().get("tier", "tier1") if metadata else "tier1"
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
        customer_id = event["data"]["object"].customer
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
    email = email.strip().lower()
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
HELP_MESSAGE = "Reply STOP to unsubscribe. Msg&Data Rates May Apply."


def twiml_response(message: str) -> Response:
    response = MessagingResponse()
    if message:
        response.message(message)
    return Response(content=str(response), media_type="application/xml")


@app.post("/sms-webhook")
async def sms_webhook(request: Request):
    payload = await request.body()
    form = parse_qs(payload.decode("utf-8"), keep_blank_values=True)

    from_number = form.get("From", [""])[0].strip()
    message_body = form.get("Body", [""])[0].strip().upper()

    db = get_db()
    try:
        row = db.execute(
            """
            SELECT email, user_phone, accountability_phone,
                   user_sms_opted_in, accountability_sms_opted_in
            FROM customers
            WHERE user_phone = ? OR accountability_phone = ?
            LIMIT 1
            """,
            (from_number, from_number),
        ).fetchone()

        if message_body in OPT_OUT_KEYWORDS:
            if row:
                email, user_phone, accountability_phone, _, _ = row
                email = email.strip().lower()
                if from_number == user_phone:
                    db.execute(
                        "UPDATE customers SET user_sms_opted_in = 0 WHERE email = ?",
                        (email,),
                    )
                elif from_number == accountability_phone:
                    db.execute(
                        "UPDATE customers SET accountability_sms_opted_in = 0 WHERE email = ?",
                        (email,),
                    )
                db.commit()
            return twiml_response(OPT_OUT_MESSAGE)

        if message_body in OPT_IN_KEYWORDS:
            if row:
                email, user_phone, accountability_phone, _, _ = row
                email = email.strip().lower()
                if from_number == user_phone:
                    db.execute(
                        "UPDATE customers SET user_sms_opted_in = 1 WHERE email = ?",
                        (email,),
                    )
                elif from_number == accountability_phone:
                    db.execute(
                        "UPDATE customers SET accountability_sms_opted_in = 1 WHERE email = ?",
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
    email = email.strip().lower()
    if not twilio_client:
        raise HTTPException(status_code=500, detail="Twilio not configured")

    db = get_db()
    row = db.execute(
        """SELECT tier, user_phone, accountability_phone,
                  user_sms_opted_in, accountability_sms_opted_in
           FROM customers WHERE email = ?""",
        (email,),
    ).fetchone()
    db.close()

    if not row:
        return {"status": "no_customer_found"}

    tier, user_phone, accountability_phone, user_sms_opted_in, accountability_sms_opted_in = row
    notified = []

    # tier1: filter-only, no texts of any kind.
    if tier == "tier1":
        return {"status": "no_notifications_for_tier1"}

    # tier2 and tier3 both text the user themselves, unless opted out.
    if user_phone and user_sms_opted_in:
        self_message = random.choice(ENCOURAGEMENT_MESSAGES)
        twilio_client.messages.create(
            to=user_phone,
            from_=TWILIO_FROM_NUMBER,
            body=f"Filtersight: {self_message} Reply STOP to opt out.",
        )
        notified.append("user")

    # Only tier3 also notifies the accountability partner, unless opted out.
    if tier == "tier3" and accountability_phone and accountability_sms_opted_in:
        twilio_client.messages.create(
            to=accountability_phone,
            from_=TWILIO_FROM_NUMBER,
            body=f"Filtersight: your accountability partner had a filter bypass attempt just now. Reply STOP to opt out.",
        )
        notified.append("partner")

    return {"status": "notified", "recipients": notified}


# ---------------------------------------------------------------------------
# 3b. Poll NextDNS logs for real blocked adult-content attempts and notify.
#
# Call this on a schedule (Railway cron, or an external service like
# cron-job.org hitting this endpoint every few minutes). It fetches recent
# NextDNS log entries, finds new ones blocked under the "porn" category,
# and fires the same tier-aware notification as /notify-attempt.
#
# TEMPORARY LIMITATION: this checks ONE NextDNS profile (NEXTDNS_PROFILE_ID)
# and routes every match to TEST_CUSTOMER_EMAIL. That's fine for proving the
# mechanism works with a single test customer, but real multi-customer
# support needs a NextDNS profile created per signup, with that profile's ID
# stored on the customer's row so each customer's own attempts route to them.
# ---------------------------------------------------------------------------
@app.post("/poll-nextdns-and-notify")
async def poll_nextdns_and_notify():
    if not NEXTDNS_API_KEY or not NEXTDNS_PROFILE_ID:
        raise HTTPException(status_code=500, detail="NextDNS not configured")

    test_customer_email = TEST_CUSTOMER_EMAIL.strip().lower() if TEST_CUSTOMER_EMAIL else None

    db = get_db()
    row = db.execute("SELECT last_checked_at FROM nextdns_state WHERE id = 1").fetchone()
    last_checked_at = row[0] if row else None
    now = datetime.datetime.utcnow().isoformat()

    try:
        resp = requests.get(
            f"https://api.nextdns.io/profiles/{NEXTDNS_PROFILE_ID}/logs",
            headers={"X-Api-Key": NEXTDNS_API_KEY},
            params={"status": "blocked", "limit": 100, "sort": "desc"},

            timeout=15,
        )
        resp.raise_for_status()
        logs = resp.json().get("data", [])
    except requests.RequestException as e:
        db.close()
        raise HTTPException(status_code=502, detail=f"Couldn't reach NextDNS: {e}")

    notified = []
    debug_blocked = []
    debug_sample = [
        {"domain": e.get("domain"), "status": e.get("status"), "timestamp": e.get("timestamp")}
        for e in logs[:5]
    ]
    for entry in logs:
        entry_time = entry.get("timestamp")
        if last_checked_at and entry_time and entry_time <= last_checked_at:
            continue

        status = entry.get("status")
        reasons = entry.get("reasons", [])
        if status == "blocked":
            debug_blocked.append({"domain": entry.get("domain"), "reasons": reasons})
        is_porn_block = status == "blocked" and any(
            "porn" in (r.get("id") or "").lower() for r in reasons
        )

        if is_porn_block and test_customer_email:
            crow = db.execute(
                """SELECT tier, user_phone, accountability_phone,
                          user_sms_opted_in, accountability_sms_opted_in
                   FROM customers WHERE email = ?""",
                (test_customer_email,),
            ).fetchone()
            if crow and twilio_client:
                tier, user_phone, accountability_phone, user_sms_opted_in, accountability_sms_opted_in = crow
                if tier != "tier1":
                    if user_phone and user_sms_opted_in:
                        self_message = random.choice(ENCOURAGEMENT_MESSAGES)
                        twilio_client.messages.create(
                            to=user_phone,
                            from_=TWILIO_FROM_NUMBER,
                            body=f"Filtersight: {self_message} Reply STOP to opt out.",
                        )
                        notified.append({"recipient": "user", "domain": entry.get("domain")})
                    if tier == "tier3" and accountability_phone and accountability_sms_opted_in:
                        twilio_client.messages.create(
                            to=accountability_phone,
                            from_=TWILIO_FROM_NUMBER,
                            body="Filtersight: your accountability partner had a filter bypass attempt just now. Reply STOP to opt out.",
                        )
                        notified.append({"recipient": "partner", "domain": entry.get("domain")})

    db.execute("UPDATE nextdns_state SET last_checked_at = ? WHERE id = 1", (now,))
    db.commit()
    db.close()
    return {
        "status": "polled",
        "notified": notified,
    }


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

REMOVAL_SILENCE_HOURS = 8  # tune based on real usage patterns once you have data

@app.post("/record-dns-activity")
async def record_dns_activity(email: str):
    """Call this from a scheduled job that polls your DNS provider's log API."""
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
async def check_for_removed_profiles():
    """Run this on a schedule (e.g. every hour) via cron or a scheduled task."""
    if not twilio_client:
        raise HTTPException(status_code=500, detail="Twilio not configured")

    db = get_db()
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=REMOVAL_SILENCE_HOURS)).isoformat()
    rows = db.execute(
        """SELECT email, tier, user_phone, accountability_phone,
                  user_sms_opted_in, accountability_sms_opted_in
           FROM customers WHERE active = 1 AND last_dns_seen < ?""",
        (cutoff,),
    ).fetchall()

    notified = []
    for email, tier, user_phone, accountability_phone, user_sms_opted_in, accountability_sms_opted_in in rows:
        if tier == "tier1":
            continue
        body = (
            f"Filtersight: it looks like the filter on {email}'s device may have been "
            f"removed or disabled — no activity in the last {REMOVAL_SILENCE_HOURS} hours."
        )
        if user_phone and user_sms_opted_in:
            twilio_client.messages.create(to=user_phone, from_=TWILIO_FROM_NUMBER, body=f"{body} Reply STOP to opt out.")
        if tier == "tier3" and accountability_phone and accountability_sms_opted_in:
            twilio_client.messages.create(to=accountability_phone, from_=TWILIO_FROM_NUMBER, body=f"{body} Reply STOP to opt out.")
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
    email = email.strip().lower()
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
                 f"Reaching out to check in is up to you. Reply STOP to opt out.",
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
