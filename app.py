import streamlit as st
import stripe
import uuid
import os
import requests

# ---------------------------------------------------------------------------
# SETUP: Set these as environment variables (never hardcode real keys in code
# that might end up in a public repo). See .env.example for the full list.
# ---------------------------------------------------------------------------
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")  # starts with sk_live_ or sk_test_
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8501")  # your real domain once deployed
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")    # where webhook_server.py runs

# Three tier price IDs — set these from your Stripe dashboard / sandbox.
STRIPE_PRICE_TIER1 = os.environ.get("STRIPE_PRICE_TIER1")  # Filter — $5/mo
STRIPE_PRICE_TIER2 = os.environ.get("STRIPE_PRICE_TIER2")  # Filter + Companion — $10/mo
STRIPE_PRICE_TIER3 = os.environ.get("STRIPE_PRICE_TIER3")  # Complete — $13/mo

TIERS = {
    "tier1": {
        "label": "Filter — $5/mo",
        "price_id": STRIPE_PRICE_TIER1,
        "description": "Filter only. Fully private. No partner, no AI companion.",
        "needs_own_phone": False,
        "needs_partner_phone": False,
        "has_chat": False,
        "has_partner": False,
    },
    "tier2": {
        "label": "Filter + Companion — $10/mo",
        "price_id": STRIPE_PRICE_TIER2,
        "description": "Filter + self-encouragement texts to your own phone + AI chat companion.",
        "needs_own_phone": True,
        "needs_partner_phone": False,
        "has_chat": True,
        "has_partner": False,
    },
    "tier3": {
        "label": "Complete — $13/mo",
        "price_id": STRIPE_PRICE_TIER3,
        "description": "Everything in Filter + Companion, plus an accountability partner is notified too.",
        "needs_own_phone": True,
        "needs_partner_phone": True,
        "has_chat": True,
        "has_partner": True,
    },
}

st.set_page_config(page_title="Filtersight", page_icon="🔒")
st.title("Filtersight")
st.write("Block adult content system-wide, with accountability built in. From $5/month.")

query_params = st.query_params
email = st.text_input("Email address")

# ---------------------------------------------------------------------------
# STEP 1: Pick a tier, then send the customer to real Stripe Checkout
# (hosted by Stripe, not built by us — this is the correct/secure way to
# collect card details).
# ---------------------------------------------------------------------------
if query_params.get("session_id") is None:
    st.subheader("Choose your plan")
    tier_key = st.radio(
        "Plan",
        options=list(TIERS.keys()),
        format_func=lambda k: TIERS[k]["label"],
    )
    st.caption(TIERS[tier_key]["description"])

    if st.button("Continue to payment"):
        selected_price_id = TIERS[tier_key]["price_id"]
        if not email:
            st.error("Enter an email first.")
        elif not stripe.api_key or not selected_price_id:
            st.error("Stripe isn't configured yet — check STRIPE_SECRET_KEY and the tier price IDs.")
        else:
            session = stripe.checkout.Session.create(
                mode="subscription",
                customer_email=email,
                line_items=[{"price": selected_price_id, "quantity": 1}],
                success_url=f"{APP_BASE_URL}/?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=APP_BASE_URL,
                metadata={"tier": tier_key},
                subscription_data={"metadata": {"tier": tier_key}},
            )
            st.link_button("Go to secure checkout", session.url)

# ---------------------------------------------------------------------------
# STEP 2: Customer lands back here after paying. We verify the session with
# Stripe directly (never trust the URL alone) before generating anything.
# ---------------------------------------------------------------------------
else:
    session_id = query_params.get("session_id")
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        paid = session.payment_status == "paid"
        customer_email = session.customer_details.email if session.customer_details else email
        tier_key = (session.metadata or {}).get("tier", "tier1")
    except Exception as e:
        paid = False
        customer_email = None
        tier_key = "tier1"
        verify_error = str(e)
    else:
        verify_error = None

    if not paid:
        st.error("We couldn't verify this payment. If you were just charged, contact support.")
        if verify_error:
            st.caption(f"Debug info: {verify_error}")
    else:
        st.success(f"Payment verified for {customer_email}. Your profile is ready.")

        def generate_mobileconfig(customer_email: str) -> str:
            payload_uuid = str(uuid.uuid4()).upper()
            top_uuid = str(uuid.uuid4()).upper()
            safe_email = customer_email.replace("@", "-at-").replace(".", "-")
            return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>PayloadContent</key>
    <array>
        <dict>
            <key>PayloadDescription</key>
            <string>Configures system-wide DNS-over-HTTPS content filtering</string>
            <key>PayloadDisplayName</key>
            <string>Filtersight</string>
            <key>PayloadIdentifier</key>
            <string>com.filtersight.filter.adult.{safe_email}</string>
            <key>PayloadType</key>
            <string>com.apple.dnsSettings.managed</string>
            <key>PayloadUUID</key>
            <string>{payload_uuid}</string>
            <key>PayloadVersion</key>
            <integer>1</integer>
            <key>DNSSettings</key>
            <dict>
                <key>DNSProtocol</key>
                <string>HTTPS</string>
                <key>ServerURL</key>
                <string>https://doh.cleanbrowsing.org/doh/adult-filter/</string>
            </dict>
        </dict>
    </array>
    <key>PayloadDisplayName</key>
    <string>Filtersight</string>
    <key>PayloadDescription</key>
    <string>System-wide DNS-over-HTTPS filter blocking adult content</string>
    <key>PayloadIdentifier</key>
    <string>com.filtersight.filter.{safe_email}</string>
    <key>PayloadOrganization</key>
    <string>Filtersight</string>
    <key>PayloadRemovalDisallowed</key>
    <false/>
    <key>PayloadType</key>
    <string>Configuration</string>
    <key>PayloadUUID</key>
    <string>{top_uuid}</string>
    <key>PayloadVersion</key>
    <integer>1</integer>
</dict>
</plist>
"""

        profile_xml = generate_mobileconfig(customer_email or email)
        st.download_button(
            label="Download Profile",
            data=profile_xml,
            file_name="filtersight.mobileconfig",
            mime="application/x-apple-aspen-config",
        )
        st.caption(
            "After download, open it from Files or the download banner in Safari, "
            "then go to Settings → General → VPN & Device Management to install."
        )

        tier_info = TIERS.get(tier_key, TIERS["tier1"])

        # -------------------------------------------------------------
        # STEP 3: Collect phone number(s), scoped to what the paid tier
        # actually needs. Tier 1 gets nothing here — it's filter-only.
        # -------------------------------------------------------------
        if tier_info["needs_own_phone"] or tier_info["needs_partner_phone"]:
            st.divider()
            st.subheader("Set up your texts")

            user_phone = None
            partner_phone = None

            if tier_info["needs_own_phone"]:
                user_phone = st.text_input(
                    "Your phone number (for encouragement texts, e.g. +15551234567)"
                )

            if tier_info["needs_partner_phone"]:
                partner_phone = st.text_input(
                    "Accountability partner's phone number (e.g. +15551234567)"
                )

            if st.button("Save phone number(s)"):
                if tier_info["needs_own_phone"] and not user_phone:
                    st.error("Enter your own phone number first.")
                elif tier_info["needs_partner_phone"] and not partner_phone:
                    st.error("Enter your accountability partner's phone number first.")
                else:
                    try:
                        resp = requests.post(
                            f"{BACKEND_URL}/save-contact",
                            params={
                                "email": customer_email or email,
                                "tier": tier_key,
                                "user_phone": user_phone or "",
                                "accountability_phone": partner_phone or "",
                            },
                            timeout=10,
                        )
                        if resp.ok:
                            st.success("Saved. You're all set.")
                        else:
                            st.error(f"Backend error: {resp.status_code} — {resp.text}")
                    except requests.RequestException as e:
                        st.error(f"Couldn't reach the backend at {BACKEND_URL}: {e}")

        # -------------------------------------------------------------
        # STEP 4: AI companion chat — Tier 2 and Tier 3 only. Tier 1
        # never sees this section at all.
        # -------------------------------------------------------------
        if tier_info["has_chat"]:
            st.divider()
            st.subheader("Talk to your companion")
            st.caption("For urges, cravings, or just talking something through. Not a general assistant.")

            if "chat_history" not in st.session_state:
                st.session_state.chat_history = []

            for msg in st.session_state.chat_history:
                with st.chat_message(msg["role"]):
                    st.write(msg["content"])

            user_message = st.chat_input("Type a message...")
            if user_message:
                st.session_state.chat_history.append({"role": "user", "content": user_message})
                with st.chat_message("user"):
                    st.write(user_message)

                with st.chat_message("assistant"):
                    with st.spinner("..."):
                        try:
                            resp = requests.post(
                                f"{BACKEND_URL}/chat",
                                json={
                                    "message": user_message,
                                    "history": st.session_state.chat_history,
                                },
                                timeout=30,
                            )
                            if resp.ok:
                                reply = resp.json().get("reply", "Sorry, something went wrong. Try again?")
                            else:
                                reply = "Couldn't reach the chat right now. Try again in a moment."
                        except requests.RequestException:
                            reply = "Couldn't reach the chat right now. Try again in a moment."
                        st.write(reply)
                st.session_state.chat_history.append({"role": "assistant", "content": reply})

        # -------------------------------------------------------------
        # STEP 5: Cancellation — available to every tier, but the path
        # differs. Tier 3 can notify their accountability partner instead
        # of paying. Tier 1 and Tier 2 always go through the small fee,
        # since they have no partner on file.
        # -------------------------------------------------------------
        st.divider()
        with st.expander("Manage subscription"):
            st.write("Canceling isn't instant — it's a small, intentional step, on purpose.")

            if tier_info["has_partner"]:
                st.write(
                    "Since you're on the Complete plan, canceling will notify your "
                    "accountability partner instead of charging a fee."
                )
                if st.button("Cancel my subscription"):
                    try:
                        resp = requests.post(
                            f"{BACKEND_URL}/request-cancellation",
                            params={
                                "email": customer_email or email,
                                "notify_contact_instead_of_paying": True,
                            },
                            timeout=10,
                        )
                        if resp.ok:
                            data = resp.json()
                            if data.get("status") == "contact_notified":
                                st.success("Your accountability partner has been notified. Cancellation will proceed after that.")
                            else:
                                st.info(str(data))
                        else:
                            st.error(f"Backend error: {resp.status_code} — {resp.text}")
                    except requests.RequestException as e:
                        st.error(f"Couldn't reach the backend at {BACKEND_URL}: {e}")
            else:
                st.write(
                    "Canceling requires a small $5 processing fee — this plan doesn't "
                    "have an accountability partner on file to notify instead."
                )
                if st.button("Proceed to cancellation fee"):
                    try:
                        resp = requests.post(
                            f"{BACKEND_URL}/request-cancellation",
                            params={
                                "email": customer_email or email,
                                "notify_contact_instead_of_paying": False,
                            },
                            timeout=10,
                        )
                        if resp.ok:
                            data = resp.json()
                            checkout_url = data.get("checkout_url")
                            if checkout_url:
                                st.link_button("Pay $5 cancellation fee", checkout_url)
                            else:
                                st.info(str(data))
                        else:
                            st.error(f"Backend error: {resp.status_code} — {resp.text}")
                    except requests.RequestException as e:
                        st.error(f"Couldn't reach the backend at {BACKEND_URL}: {e}")
