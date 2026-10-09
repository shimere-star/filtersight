import streamlit as st
import stripe
import uuid
import os
import requests
import traceback
import time

from companion_flow import (
    CHECKIN_OPTIONS,
    FOLLOWUP_OPTIONS,
    FOLLOWUP_RESPONSES,
    INTERVENTIONS,
    route_checkin,
)

# ---------------------------------------------------------------------------
# SETUP: Set these as environment variables (never hardcode real keys in code
# that might end up in a public repo). See .env.example for the full list.
# ---------------------------------------------------------------------------
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")  # starts with sk_live_ or sk_test_
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8501")  # your real domain once deployed
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")    # where webhook_server.py runs

# One current plan and one current price configuration path.
PLAN_ID = "filtersight"
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")

st.set_page_config(page_title="FilterSight", page_icon="🔒")
st.markdown(
    """
    <style>
    :root { --fs-ink:#1B2430; --fs-paper:#F7F8F7; --fs-teal:#2F6F6B; --fs-line:#D8DEDC; }
    .stApp { background:var(--fs-paper); color:var(--fs-ink); }
    .main .block-container { max-width:760px; padding-left:1rem; padding-right:1rem; }
    .stButton > button, .stLinkButton > a { min-height:44px; border-radius:8px; }
    .stButton > button:focus-visible, .stLinkButton > a:focus-visible, a:focus-visible {
        outline:3px solid #0B5FFF !important; outline-offset:2px;
    }
    div[role="radiogroup"] label { min-height:44px; padding:.35rem 0; }
    .help-now { margin:.25rem 0 1.25rem; }
    .help-now a { color:#204F4C; font-weight:700; text-decoration:underline; }
    .session-note { color:#4A5561; font-size:.9rem; }
    .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px;
      overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
    @media (max-width:480px) {
      .main .block-container { width:100%; max-width:100%; padding:1rem; }
      .stButton > button, .stLinkButton > a { width:100%; max-width:100%; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)
query_params = st.query_params
is_member_view = query_params.get("view") == "member"
if not is_member_view:
    st.title("FilterSight")
    st.write("Private DNS filtering and calm support when you need it.")
    email = st.text_input("Email address")
    normalized_email = email.strip().lower()
else:
    normalized_email = ""


def clear_checkin_state():
    for key in list(st.session_state.keys()):
        if key.startswith("checkin_"):
            st.session_state.pop(key, None)


def go_to_checkin(screen: str):
    st.session_state.pop("checkin_timer_started", None)
    st.session_state.pop("checkin_exercise_result", None)
    st.session_state.checkin_screen = screen
    st.rerun()


def render_help_action():
    if st.button("I need help now", key=f"help_{st.session_state.get('checkin_screen', 'dashboard')}"):
        go_to_checkin("emergency")


def render_numbered_steps(steps):
    for number, step in enumerate(steps, start=1):
        st.write(f"{number}. {step}")


def render_companion(headers):
    st.title("Talk it through")
    render_help_action()
    st.write("Have a private conversation with the AI companion. No judgment — just a calm place to put what's happening into words.")
    st.info("Your current message and relevant recent conversation history are sent to Anthropic to generate a reply. FilterSight does not save companion conversations in its database.")
    if "member_chat_history" not in st.session_state:
        st.session_state.member_chat_history = [{"role": "assistant", "content": "I'm here. What's on your mind?"}]
    for item in st.session_state.member_chat_history:
        with st.chat_message(item["role"]):
            st.write(item["content"])
    prompt = st.chat_input("What’s going on right now?")
    if prompt:
        history = st.session_state.member_chat_history[-20:]
        st.session_state.member_chat_history.append({"role": "user", "content": prompt})
        try:
            response = requests.post(
                f"{BACKEND_URL}/member/chat", headers=headers,
                json={"message": prompt, "history": history}, timeout=30,
            )
            response.raise_for_status()
            reply = response.json().get("reply", "I'm here. Can you tell me a bit more about what's going on?")
        except requests.RequestException:
            reply = "I couldn't connect just now. Please try again in a moment."
        st.session_state.member_chat_history.append({"role": "assistant", "content": reply})
        st.rerun()
    if st.button("End conversation"):
        st.session_state.pop("member_chat_history", None)
        go_to_checkin("chat_complete")


def render_emergency():
    st.title("If you need help right now")
    st.markdown(
        '<div class="sr-only" role="status" aria-live="assertive">If you need help right now. FilterSight and its AI companion are not emergency or professional mental-health services.</div>',
        unsafe_allow_html=True,
    )
    st.write("FilterSight and its AI companion are not emergency or professional mental-health services. This screen can't replace professional care.")
    st.error("If you or someone else may be in immediate danger, contact local emergency services now.")
    st.write("You can also reach out to a trusted person on your own device.")
    st.write("In the United States and Canada, you can call or text **988** any time. It's free and confidential.")
    st.write("If you're somewhere else, contact your local crisis line or emergency services.")
    st.write("You don't have to face this alone.")
    st.markdown('<a href="tel:988" aria-label="Call 988 (US & Canada)">Call 988 (US & Canada)</a>', unsafe_allow_html=True)
    if st.button("Back to check-in"):
        go_to_checkin("checkin")


def render_followup():
    st.title("Check back in")
    render_help_action()
    choice = st.radio("How are you feeling now?", FOLLOWUP_OPTIONS, index=None, key="checkin_followup_choice")
    if st.button("Continue", key="followup_continue"):
        if not choice:
            st.error("Pick the option that's closest.")
        elif choice == FOLLOWUP_OPTIONS[4]:
            go_to_checkin("chat")
        elif choice == FOLLOWUP_OPTIONS[5]:
            go_to_checkin("emergency")
        else:
            st.session_state.checkin_followup_result = choice
            st.rerun()
    result = st.session_state.get("checkin_followup_result")
    if result in FOLLOWUP_RESPONSES:
        message, actions = FOLLOWUP_RESPONSES[result]
        st.info(message)
        for action in actions:
            if st.button(action, key=f"followup_action_{action}"):
                if action in ("Talk more", "Talk with companion"):
                    go_to_checkin("chat")
                elif action == "Emergency support":
                    go_to_checkin("emergency")
                elif action == "Try another exercise":
                    go_to_checkin("interventions")
                elif action == "Start a cooldown":
                    go_to_checkin("interventions")
                else:
                    clear_checkin_state()
                    st.rerun()


def render_exercise(exercise_key: str):
    exercise = INTERVENTIONS[exercise_key]
    st.title(exercise["title"])
    render_help_action()
    st.write(exercise["intro"])
    render_numbered_steps(exercise["steps"])
    st.markdown('<p class="session-note">This runs in your current session only — it isn\'t saved.</p>', unsafe_allow_html=True)
    if "minutes" in exercise:
        started_key = "checkin_timer_started"
        if started_key not in st.session_state:
            st.session_state[started_key] = time.monotonic()

        @st.fragment(run_every="30s")
        def timer_status():
            elapsed = max(0, time.monotonic() - st.session_state[started_key])
            total = exercise["minutes"] * 60
            shown_minute = min(exercise["minutes"], int(elapsed // 60) + 1)
            st.info(f"{shown_minute} of {exercise['minutes']} minutes", icon="⏱️")
            if elapsed >= total / 2 and elapsed < total:
                st.write(exercise["midpoint"])
            if elapsed >= total:
                st.session_state.checkin_exercise_result = "complete"
                st.rerun(scope="app")
        timer_status()

    result = st.session_state.get("checkin_exercise_result")
    if result:
        st.success(exercise["completion"] if result == "complete" else exercise["stop"])
        if st.button("Continue", key="exercise_continue"):
            st.session_state.pop("checkin_timer_started", None)
            st.session_state.pop("checkin_exercise_result", None)
            go_to_checkin("followup")
        return
    if "minutes" not in exercise and st.button("Finish exercise"):
        st.session_state.checkin_exercise_result = "complete"
        st.rerun()
    if st.button("Stop", key="exercise_stop"):
        st.session_state.checkin_exercise_result = "stopped"
        st.rerun()


def render_checkin_flow(headers):
    screen = st.session_state.get("checkin_screen")
    if screen == "emergency":
        render_emergency()
    elif screen == "chat":
        render_companion(headers)
    elif screen == "chat_complete":
        st.title("Conversation ended")
        render_help_action()
        st.write("Thanks for talking it through. You can come back anytime.")
        st.write("You can leave the conversation whenever you like — nothing is held against you.")
        if st.button("Done"):
            clear_checkin_state()
            st.rerun()
    elif screen == "checkin":
        st.title("Check in")
        render_help_action()
        st.write("Pick the closest one. There's no wrong answer.")
        choice = st.radio("What's happening right now?", CHECKIN_OPTIONS, index=None, key="checkin_choice")
        if st.button("Continue", key="checkin_continue"):
            if not choice:
                st.error("Pick the option that's closest.")
            else:
                go_to_checkin(route_checkin(choice))
        if st.button("Done", key="checkin_done"):
            clear_checkin_state()
            st.rerun()
    elif screen == "interventions":
        st.title("Pick something small")
        render_help_action()
        st.write("You don't have to fix everything. Just try one small thing.")
        for key, item in INTERVENTIONS.items():
            if st.button(f"{item['title']} — {item['description']}", key=f"choose_{key}"):
                st.session_state.checkin_exercise = key
                go_to_checkin("exercise")
        if st.button("Talk it through — Have a private conversation with the AI companion."):
            go_to_checkin("chat")
        if st.button("Back", key="interventions_back"):
            go_to_checkin("checkin")
    elif screen == "exercise":
        render_exercise(st.session_state["checkin_exercise"])
    elif screen == "followup":
        render_followup()
    elif screen == "accidental":
        st.title("That happens")
        render_help_action()
        st.write("Blocks sometimes catch the wrong page. FilterSight does not retain DNS query logs.")
        st.write("If a site you need is blocked, email support@filtersight.com and we'll take a look.")
        if st.button("Done"):
            clear_checkin_state()
            st.rerun()
    elif screen == "legitimate":
        st.title("Site exceptions")
        render_help_action()
        st.write("FilterSight doesn't currently offer a way to allow individual blocked sites. If you need a blocked site for work or school, email support@filtersight.com and we'll take a look.")
        if st.button("Done"):
            clear_checkin_state()
            st.rerun()


def render_member_dashboard():
    token = st.session_state.get("member_access_token")
    if not token:
        st.title("Your FilterSight dashboard")
        st.caption("Sign in with the email address on your subscription.")
    magic_token = query_params.get("magic_token")
    if not token and magic_token:
        st.info("Your sign-in link is ready. Select below to finish signing in.")
        if st.button("Sign in to FilterSight"):
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

    if not st.session_state.get("checkin_screen"):
        st.title("Your FilterSight dashboard")

    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(f"{BACKEND_URL}/member/profile", headers=headers, timeout=15)
        response.raise_for_status()
        profile = response.json()
    except requests.RequestException:
        st.session_state.pop("member_access_token", None)
        st.error("Your sign-in expired or your subscription could not be verified. Request a new link below.")
        st.rerun()

    st.success(f"Signed in as {profile['email']} · FilterSight")
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

    st.divider()
    if st.session_state.get("checkin_screen"):
        render_checkin_flow(headers)
    else:
        st.subheader("Support in the moment")
        st.caption("A calm, private place to pause, reset, or talk things through.")
        if st.button("Check in", type="primary"):
            go_to_checkin("checkin")
        if st.button("Talk it through"):
            go_to_checkin("chat")

    if st.button("Sign out"):
        try:
            requests.post(f"{BACKEND_URL}/member/logout", headers=headers, timeout=10)
        except requests.RequestException:
            pass
        st.session_state.pop("member_access_token", None)
        st.session_state.pop("member_chat_history", None)
        clear_checkin_state()
        st.rerun()

# ---------------------------------------------------------------------------
# STEP 1: Send the customer to real Stripe Checkout
# (hosted by Stripe, not built by us — this is the correct/secure way to
# collect card details).
# ---------------------------------------------------------------------------
if query_params.get("view") == "member":
    render_member_dashboard()
elif query_params.get("session_id") is None:
    st.subheader("FilterSight")
    st.write("$10/month")
    st.caption("Private DNS filtering plus the AI companion and guided check-ins. Cancel anytime.")
    if st.button("Continue to payment"):
        if not normalized_email:
            st.error("Enter an email first.")
        elif not stripe.api_key or not STRIPE_PRICE_ID:
            st.error("Stripe isn't configured yet — check STRIPE_SECRET_KEY and STRIPE_PRICE_ID.")
        else:
            session = stripe.checkout.Session.create(
                mode="subscription",
                customer_email=normalized_email,
                line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
                success_url=f"{APP_BASE_URL}/?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=APP_BASE_URL,
                metadata={"plan": PLAN_ID},
                subscription_data={"metadata": {"plan": PLAN_ID}},
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
        plan_id = session.metadata.to_dict().get("plan") if session.metadata else None
        paid = paid and plan_id == PLAN_ID
    except Exception as e:
        traceback.print_exc()
        paid = False
        customer_email = None
        plan_id = None
        verify_error = str(e)
    else:
        verify_error = None

    if not paid:
        st.error("We couldn't verify this payment. If you were just charged, contact support.")
        if verify_error:
            st.caption(f"Debug info: {verify_error}")
    else:
        st.success(f"Payment verified for {customer_email}. Preparing your profile…")
        st.link_button("Open your member dashboard", f"{APP_BASE_URL}/?view=member")

        try:
            profile_response = requests.post(
                f"{BACKEND_URL}/provision-nextdns-profile",
                json={"checkout_session_id": session_id},
                timeout=90,
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
            <string>FilterSight</string>
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
    <string>FilterSight</string>
    <key>PayloadDescription</key>
    <string>System-wide DNS-over-HTTPS content filtering</string>
    <key>PayloadIdentifier</key>
    <string>com.filtersight.filter.{safe_email}</string>
    <key>PayloadOrganization</key>
    <string>FilterSight</string>
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


        st.info("Sign in to your member dashboard to manage your subscription and use your companion.")
