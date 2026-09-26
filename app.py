import streamlit as st
import stripe
import uuid
import os
import requests
import traceback

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
ENABLE_TIER2_TIER3 = os.environ.get("ENABLE_TIER2_TIER3", "false").lower() in ("1", "true", "yes")

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
AVAILABLE_TIERS = TIERS if ENABLE_TIER2_TIER3 else {"tier1": TIERS["tier1"]}

st.set_page_config(page_title="Filtersight", page_icon="🔒")
st.title("Filtersight")
st.write("Block adult content system-wide, with accountability built in. From $5/month.")

query_params = st.query_params
email = st.text_input("Email address")
normalized_email = email.strip().lower()


def render_member_dashboard():
    st.title("Your Filtersight dashboard")
    st.caption("Sign in with the email address on your subscription.")

    token = st.session_state.get("member_access_token")
    magic_token = query_params.get("magic_token")
    if not token and magic_token:
        st.info("Your sign-in link is ready. Select below to finish signing in.")
        if st.button("Sign in to Filtersight"):
            try:
                response = requests.post(
                    f"{BACKEND_URL}/member/verify-link",
                    json={"token": magic_token},
                    timeout=15,
                )
                response.raise_for_status()
                st.session_state.member_access_token = response.json()["access_token"]
                del query_params["magic_token"]
                st.rerun()
            except requests.RequestException:
                st.error("That sign-in link expired or has already been used. Request a new one below.")
        st.divider()

    token = st.session_state.get("member_access_token")
    if not token:
        member_email = st.text_input("Subscription email", key="member_email")
        if st.button("Email me a sign-in link"):
            if not member_email.strip():
                st.error("Enter the email address used at checkout.")
            else:
                try:
                    response = requests.post(
                        f"{BACKEND_URL}/member/request-link",
                        json={"email": member_email.strip().lower()},
                        timeout=15,
                    )
                    if response.ok:
                        st.success("If that address has an active subscription, we sent a sign-in link. Check your inbox and spam folder.")
                    elif response.status_code == 503:
                        st.error("Email sign-in is not configured yet. Contact support for account help.")
                    else:
                        st.error("We couldn't send a sign-in link right now. Please try again later.")
                except requests.RequestException:
                    st.error("We couldn't reach account sign-in. Please try again later.")
        return

    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(f"{BACKEND_URL}/member/profile", headers=headers, timeout=15)
        response.raise_for_status()
        profile = response.json()
    except requests.RequestException:
        st.session_state.pop("member_access_token", None)
        st.error("Your sign-in expired or your subscription could not be verified. Request a new link below.")
        st.rerun()

    st.success(f"Signed in as {profile['email']} · {profile['tier'].replace('tier', 'Tier ')}")
    if profile["cancel_at_period_end"]:
        st.info("Your subscription is set to end at the close of the current billing period. No cancellation fee is charged.")
    else:
        with st.expander("Manage subscription"):
            st.write("Your plan stays active through the current billing period. Canceling has no fee.")
            if st.button("Cancel my subscription"):
                try:
                    response = requests.post(f"{BACKEND_URL}/member/cancel", headers=headers, timeout=15)
                    response.raise_for_status()
                    st.success("Cancellation scheduled for the end of your current billing period. No fee was charged.")
                    st.rerun()
                except requests.RequestException:
                    st.error("We couldn't schedule cancellation right now. Please try again.")

    if profile["has_chat"]:
        st.divider()
        st.subheader("Filtersight companion")
        st.caption("A calm, focused place to talk through urges and coping in the moment.")
        if "member_chat_history" not in st.session_state:
            st.session_state.member_chat_history = []
        for item in st.session_state.member_chat_history:
            with st.chat_message(item["role"]):
                st.write(item["content"])
        prompt = st.chat_input("What’s going on right now?")
        if prompt:
            history = st.session_state.member_chat_history[-20:]
            st.session_state.member_chat_history.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.write(prompt)
            try:
                response = requests.post(
                    f"{BACKEND_URL}/member/chat",
                    headers=headers,
                    json={"message": prompt, "history": history},
                    timeout=30,
                )
                response.raise_for_status()
                reply = response.json().get("reply", "I'm here. Can you tell me a bit more about what's going on?")
            except requests.RequestException:
                reply = "I couldn't connect just now. Please try again in a moment."
            st.session_state.member_chat_history.append({"role": "assistant", "content": reply})
            with st.chat_message("assistant"):
                st.write(reply)
    else:
        st.info("The AI companion is included with Tier 2 and Tier 3. Your current plan includes subscription management here.")

    if st.button("Sign out"):
        try:
            requests.post(f"{BACKEND_URL}/member/logout", headers=headers, timeout=10)
        except requests.RequestException:
            pass
        st.session_state.pop("member_access_token", None)
        st.session_state.pop("member_chat_history", None)
        st.rerun()

# ---------------------------------------------------------------------------
# STEP 1: Pick a tier, then send the customer to real Stripe Checkout
# (hosted by Stripe, not built by us — this is the correct/secure way to
# collect card details).
# ---------------------------------------------------------------------------
if query_params.get("view") == "member":
    render_member_dashboard()
elif query_params.get("session_id") is None:
    st.subheader("Choose your plan")
    tier_key = st.radio(
        "Plan",
        options=list(AVAILABLE_TIERS.keys()),
        format_func=lambda k: TIERS[k]["label"],
    )
    st.caption(TIERS[tier_key]["description"])
    if st.button("Continue to payment"):
        selected_price_id = TIERS[tier_key]["price_id"]
        if not normalized_email:
            st.error("Enter an email first.")
        elif not stripe.api_key or not selected_price_id:
            st.error("Stripe isn't configured yet — check STRIPE_SECRET_KEY and the tier price IDs.")
        else:
            session = stripe.checkout.Session.create(
                mode="subscription",
                customer_email=normalized_email,
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
        customer_email = (
            session.customer_details.email.strip().lower()
            if session.customer_details and session.customer_details.email
            else normalized_email
        )
        tier_key = session.metadata.to_dict().get("tier", "tier1") if session.metadata else "tier1"
    except Exception as e:
        traceback.print_exc()
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
        if tier_key in ("tier2", "tier3") and not ENABLE_TIER2_TIER3:
            st.error("This plan isn't available yet. Your payment is being reviewed; contact support if you were charged.")
            st.stop()
        st.success(f"Payment verified for {customer_email}. Preparing your profile…")
        st.link_button("Open your member dashboard", f"{APP_BASE_URL}/?view=member")

        try:
            profile_response = requests.post(
                f"{BACKEND_URL}/provision-nextdns-profile",
                json={"checkout_session_id": session_id},
                timeout=30,
            )
            profile_response.raise_for_status()
            nextdns_profile_id = profile_response.json()["profile_id"]
        except (requests.RequestException, KeyError, ValueError) as e:
            st.error("We verified your payment but couldn't prepare your DNS profile. Please contact support; you won't be charged again by retrying this page.")
            st.stop()

        def generate_mobileconfig(customer_email: str, nextdns_profile_id: str) -> str:
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
                <string>https://dns.nextdns.io/{nextdns_profile_id}</string>
            </dict>
        </dict>
    </array>
    <key>PayloadDisplayName</key>
    <string>Filtersight</string>
    <key>PayloadDescription</key>
    <string>System-wide DNS-over-HTTPS content filtering</string>
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

        profile_xml = generate_mobileconfig(customer_email or normalized_email, nextdns_profile_id)
        st.download_button(
            label="Download Profile",
            data=profile_xml,
            file_name="filtersight.mobileconfig",
            mime="application/x-apple-aspen-config",
        )
        st.caption("After download, open it from Files or Safari, then install it from Settings → General → VPN & Device Management.")

        tier_info = TIERS.get(tier_key, TIERS["tier1"])
        if tier_info["has_chat"]:
            st.info(
                "For Tier 2 and Tier 3, NextDNS query logs are used to detect blocked adult-content attempts. "
                "Those logs can include blocked domain names and timestamps; NextDNS applies the profile's log-retention setting."
            )

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

            sms_opt_in = st.checkbox(
                "I agree to receive recurring SMS messages from Filtersight for encouragement and account support. Message frequency varies. Msg & data rates may apply. Reply STOP to opt out, HELP for help. Consent isn't required to buy the plan.",
                value=False,
            )
            st.caption("Read our [Privacy Policy](https://filtersight.com/privacy.html) and [Terms](https://filtersight.com/terms.html).")

            st.caption("Your accountability partner must reply YES to their own invitation before receiving any alerts. Their consent is collected separately.")

            if st.button("Save phone number(s)"):
                if sms_opt_in and not user_phone:
                    st.error("Enter your phone number to receive SMS messages.")
                elif tier_info["needs_partner_phone"] and not partner_phone:
                    st.error("Enter your accountability partner's phone number to send their opt-in invitation.")
                else:
                    try:
                        resp = requests.post(
                            f"{BACKEND_URL}/save-contact",
                            json={
                                "checkout_session_id": session_id,
                                "user_phone": user_phone or "",
                                "accountability_phone": partner_phone or "",
                                "user_sms_opted_in": bool(sms_opt_in),
                            },
                            timeout=10,
                        )
                        if resp.ok:
                            st.success("Saved. If you added an accountability partner, they must reply YES before receiving alerts.")
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
                                    "history": st.session_state.chat_history[:-1],
                                    "checkout_session_id": session_id,
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
        # STEP 5: Cancellation — available to every tier.
        # -------------------------------------------------------------
        st.divider()
        with st.expander("Manage subscription"):
            st.write("Your subscription will remain active until the end of the current billing period. There is no cancellation fee.")

            if st.button("Cancel my subscription"):
                try:
                    resp = requests.post(
                        f"{BACKEND_URL}/request-cancellation",
                        params={
                            "checkout_session_id": session_id,
                        },
                        timeout=10,
                    )
                    if resp.ok:
                        data = resp.json()
                        status = data.get("status")
                        if status == "cancelled":
                            st.success("Your subscription has been cancelled.")
                        elif status == "cancellation_scheduled":
                            st.success("Your cancellation is scheduled for the end of your current billing period. No fee was charged.")
                        else:
                            st.info(str(data))
                    else:
                        st.error(f"Backend error: {resp.status_code} — {resp.text}")
                except requests.RequestException as e:
                    st.error(f"Couldn't reach the backend at {BACKEND_URL}: {e}")
