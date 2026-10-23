import streamlit as st
import stripe
import uuid
import os
import json
import requests
import logging
import time
import calendar
import datetime
from html import escape

from companion_flow import (
    CHECKIN_OPTIONS,
    FOLLOWUP_OPTIONS,
    FOLLOWUP_RESPONSES,
    INTERVENTIONS,
    route_checkin,
)

logger = logging.getLogger(__name__)

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

JOURNAL_CATEGORIES = {
    "craving_or_temptation": "Craving or temptation",
    "stress_or_anxiety": "Stress or anxiety",
    "boredom": "Boredom",
    "loneliness": "Loneliness",
    "accidental_block": "Accidental block",
    "legitimate_access_need": "Legitimate access need",
    "something_else": "Something else",
}
JOURNAL_ACTIONS = {
    "breathing_exercise": "Breathing exercise",
    "grounding_exercise": "Grounding exercise",
    "distraction": "Distraction",
    "five_minute_cooldown": "Five-minute cooldown",
    "ten_minute_cooldown": "Ten-minute cooldown",
    "talked_it_through": "Talked it through",
    "contacted_trusted_person": "Contacted a trusted person",
    "did_something_else": "Did something else",
}
DAILY_GOALS = {
    "porn_free": "Porn-free day",
    "masturbation_free": "Masturbation-free day",
    "porn_and_masturbation_free": "Porn- and masturbation-free day",
}
SOCIAL_GOALS = {
    "say_hello": "Say hello to a woman in an everyday setting",
    "short_conversation": "Have a short, ordinary conversation with a woman",
    "ask_open_question": "Ask an open question and listen to the answer",
    "listen_and_follow_up": "Practice listening and ask one follow-up question",
    "invite_shared_activity": "Invite someone you know to a casual shared activity",
}

st.set_page_config(page_title="FilterSight", page_icon="🔒")
st.markdown(
    """
    <style>
    :root { --fs-ink:#1B2430; --fs-paper:#F7F8F7; --fs-teal:#2F6F6B; --fs-line:#D8DEDC; }
    .stApp { background:var(--fs-paper); color:var(--fs-ink); }
.main .block-container {
    width:100%; max-width:760px; padding-left:1rem; padding-right:1rem;
    box-sizing:border-box;
}
.stButton > button, .stLinkButton > a, .stFormSubmitButton > button, .stDownloadButton > button {
    min-height:44px; border-radius:8px; color:#FAFAFA !important;
}
.stButton > button:disabled {
    background:#E5E7EB !important; border-color:#CBD5E1 !important;
    color:#4A5561 !important; opacity:1 !important;
}
    .stButton > button:focus-visible, .stLinkButton > a:focus-visible, a:focus-visible {
        outline:3px solid #0B5FFF !important; outline-offset:2px;
    }
    [data-testid="stWidgetLabel"], [data-testid="stWidgetLabel"] p,
    div[role="radiogroup"] label, div[role="radiogroup"] label p {
        color:var(--fs-ink) !important;
    }
    div[role="radiogroup"] {
        gap:.65rem;
    }
    div[role="radiogroup"] label {
        min-height:62px;
        padding:.85rem 1rem;
        margin:0 0 .2rem;
        border:1px solid #D5DDD8;
        border-radius:14px;
        background:#FFFFFF;
        box-shadow:0 2px 10px rgba(18, 45, 37, .04);
        transition:border-color .16s ease, background .16s ease, box-shadow .16s ease, transform .16s ease;
        cursor:pointer;
    }
    div[role="radiogroup"] label:hover {
        border-color:#7CA795;
        background:#F8FCF9;
        transform:translateY(-1px);
        box-shadow:0 5px 16px rgba(18, 45, 37, .08);
    }
    div[role="radiogroup"] label:has(input:checked) {
        border-color:#2F6F5E;
        background:#E5F4EC;
        box-shadow:0 0 0 2px rgba(47,111,94,.13);
    }
    div[role="radiogroup"] input[type="radio"] { accent-color:#2F6F5E; }
    .checkin-hero {
        position:relative;
        overflow:hidden;
        margin:.25rem 0 1.15rem;
        padding:2.2rem 2.25rem 2rem;
        border-radius:24px;
        background:linear-gradient(145deg,#102821 0%,#183D32 100%);
        color:#FFF9EF;
        box-shadow:0 18px 44px rgba(15,39,32,.16);
    }
    .checkin-hero::after {
        content:"";
        position:absolute;
        width:180px; height:180px;
        right:-70px; top:-85px;
        border-radius:50%;
        border:34px solid rgba(164,220,190,.12);
    }
    .checkin-kicker {
        display:inline-flex;
        align-items:center;
        gap:.45rem;
        margin-bottom:.85rem;
        color:#B9E4CE;
        font-size:.78rem;
        font-weight:800;
        letter-spacing:.12em;
        text-transform:uppercase;
    }
    .checkin-kicker::before {
        content:"";
        width:8px; height:8px;
        border-radius:50%;
        background:#F08A72;
        box-shadow:0 0 0 4px rgba(240,138,114,.16);
    }
    .checkin-hero h1 {
        position:relative;
        z-index:1;
        margin:0 0 .65rem;
        max-width:560px;
        color:#FFF9EF !important;
        font-family:Georgia,"Times New Roman",serif;
        font-size:clamp(2.25rem,7vw,4rem);
        line-height:.98;
        letter-spacing:-.045em;
    }
    .checkin-hero p {
        position:relative;
        z-index:1;
        margin:0;
        max-width:520px;
        color:#D9E8E1 !important;
        font-size:1.05rem;
        line-height:1.6;
    }
    .checkin-guidance {
        display:flex;
        gap:.75rem;
        align-items:flex-start;
        margin:1.15rem 0 1.25rem;
        padding:1rem 1.05rem;
        border:1px solid #D7E5DD;
        border-radius:14px;
        background:#F0F7F3;
        color:#29443A;
    }
    .checkin-guidance strong { color:#173C31; }
    .checkin-guidance-icon {
        flex:0 0 auto;
        width:28px; height:28px;
        display:grid; place-items:center;
        border-radius:50%;
        background:#CDEAD9;
        color:#1F5A49;
        font-weight:800;
    }
    .checkin-section-label {
        margin:1.35rem 0 .35rem;
        color:#173C31;
        font-size:.82rem;
        font-weight:800;
        letter-spacing:.1em;
        text-transform:uppercase;
    }
    .help-now { margin:.25rem 0 1.25rem; }
    .help-now a { color:#204F4C; font-weight:700; text-decoration:underline; }
    .session-note { color:#4A5561; font-size:.9rem; }
.journal-intro { border-left:4px solid var(--fs-teal); padding:.15rem 0 .15rem 1rem; margin:0 0 1.5rem; }
.journal-intro strong { color:var(--fs-teal); font-size:.9rem; }
.journal-hero {
    position:relative;
    overflow:hidden;
    margin:.35rem 0 1rem;
    padding:2.5rem 2.35rem 2.2rem;
    border-radius:24px;
    background:
        radial-gradient(circle at 88% 8%, rgba(181,225,198,.17) 0 14%, transparent 14.5%),
        linear-gradient(145deg,#102821 0%,#183D32 100%);
    color:#FFF9EF;
    box-shadow:0 20px 48px rgba(15,39,32,.17);
}
.journal-hero::before {
    content:"";
    position:absolute;
    width:210px; height:210px;
    right:-95px; bottom:-125px;
    border-radius:50%;
    border:38px solid rgba(240,138,114,.10);
}
.journal-kicker {
    display:inline-flex;
    align-items:center;
    gap:.5rem;
    margin-bottom:.9rem;
    color:#B9E4CE;
    font-size:.78rem;
    font-weight:800;
    letter-spacing:.13em;
    text-transform:uppercase;
}
.journal-kicker::before {
    content:"";
    width:8px; height:8px;
    border-radius:50%;
    background:#F08A72;
    box-shadow:0 0 0 4px rgba(240,138,114,.15);
}
.journal-hero h1 {
    position:relative;
    z-index:1;
    margin:0 0 .7rem;
    color:#FFF9EF !important;
    font-family:Georgia,"Times New Roman",serif;
    font-size:clamp(2.4rem,7vw,4.2rem);
    line-height:1;
    letter-spacing:-.045em;
}
.journal-hero p {
    position:relative;
    z-index:1;
    max-width:540px;
    margin:0;
    color:#D9E8E1 !important;
    font-size:1.02rem;
    line-height:1.65;
}
.journal-trust {
    display:grid;
    grid-template-columns:repeat(3,1fr);
    gap:.7rem;
    margin:0 0 1.8rem;
}
.journal-trust-item {
    padding:.85rem .9rem;
    border:1px solid #D7E5DD;
    border-radius:13px;
    background:#F0F7F3;
    color:#29443A;
    font-size:.83rem;
    line-height:1.35;
}
.journal-trust-item strong {
    display:block;
    margin-bottom:.16rem;
    color:#173C31;
    font-size:.88rem;
}
.journal-section-heading { margin:2rem 0 1rem; }
.journal-section-heading .eyebrow {
    margin:0 0 .35rem;
    color:#2F6F5E;
    font-size:.75rem;
    font-weight:800;
    letter-spacing:.12em;
    text-transform:uppercase;
}
.journal-section-heading h2 {
    margin:0 0 .35rem;
    color:#17251F !important;
    font-family:Georgia,"Times New Roman",serif;
    font-size:2rem;
    letter-spacing:-.025em;
}
.journal-section-heading p {
    margin:0;
    color:#5E6B65 !important;
    line-height:1.55;
}
[data-testid="stForm"] {
    padding:1.35rem 1.4rem 1.45rem;
    border:1px solid #DCE4DF;
    border-radius:18px;
    background:#FFFFFF;
    box-shadow:0 8px 24px rgba(18,45,37,.06);
}
[data-testid="stExpander"] {
    overflow:hidden;
    border:1px solid #DCE4DF !important;
    border-radius:14px !important;
    background:#FFFFFF;
    box-shadow:0 4px 16px rgba(18,45,37,.04);
}
[data-testid="stExpander"] summary:hover { background:#F1F7F3; }
.journal-data-note {
    margin:.6rem 0 1rem;
    padding:1rem 1.05rem;
    border-left:4px solid #F08A72;
    border-radius:0 12px 12px 0;
    background:#FFF4EF;
    color:#5D3B32;
    font-size:.92rem;
    line-height:1.5;
}
.stTextInput input, .stTextArea textarea {
    background:#FFFFFF !important; color:var(--fs-ink) !important;
}
div[data-baseweb="input"], div[data-baseweb="textarea"] { background:#FFFFFF !important; }
[data-testid="stChatMessage"],
[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"],
[data-testid="stChatMessage"] p {
    color:var(--fs-ink) !important;
}
.sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px;
      overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
    @media (max-width:480px) {
      .main .block-container { width:100%; max-width:100%; padding:1rem; }
      .stButton > button, .stLinkButton > a { width:100%; max-width:100%; }
      .checkin-hero { padding:1.7rem 1.25rem 1.55rem; border-radius:18px; }
      .checkin-hero h1 { font-size:2.5rem; }
      .checkin-hero p { font-size:.98rem; }
      .journal-hero { padding:1.75rem 1.25rem 1.6rem; border-radius:18px; }
      .journal-hero h1 { font-size:2.65rem; }
      .journal-trust { grid-template-columns:1fr; gap:.5rem; }
      [data-testid="stForm"] { padding:1rem; border-radius:14px; }
      div[role="radiogroup"] label { padding:.8rem .85rem; }
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


def clear_journal_state():
    for key in list(st.session_state.keys()):
        if key.startswith("journal_"):
            st.session_state.pop(key, None)


def go_to_member_screen(screen=None):
    st.session_state.member_screen = screen
    st.rerun()


def journal_error_message(error):
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status == 429:
        return "You've made several changes recently. Wait a little, then try again."
    if status == 503:
        return "Your private journal isn't available right now. Please try again later."
    if status in (401, 403):
        return "Your sign-in is no longer valid. Sign in again to continue."
    if status == 422:
        return "Check the entry limits and remove any website address, then try again."
    return "We couldn't complete that journal request. Please try again."


def journal_get(path, headers, **kwargs):
    response = requests.get(f"{BACKEND_URL}/member/journal{path}", headers=headers, timeout=15, **kwargs)
    response.raise_for_status()
    return response.json()


def journal_write(method, path, headers, **kwargs):
    request_method = getattr(requests, method)
    response = request_method(
        f"{BACKEND_URL}/member/journal{path}", headers=headers, timeout=15, **kwargs
    )
    response.raise_for_status()
    return response.json()


def entry_heading(entry):
    created = (entry.get("created_at") or "").replace("T", " ").replace(".000000Z", " UTC")
    category = JOURNAL_CATEGORIES.get(entry.get("category"), "Journal entry")
    return f"{category} · {created}" if created else category


def journal_option_label(value, labels):
    return "Not selected" if value is None else labels.get(value, "Other")


def refresh_journal_entries():
    for key in ("journal_entries_loaded", "journal_entries", "journal_next_cursor"):
        st.session_state.pop(key, None)


def render_journal_plan(headers):
    st.markdown(
        """
        <div class="journal-panel-intro">
            <span class="eyebrow">Your north star</span>
            <h2>Your plan</h2>
            <p>Keep one meaningful goal and a few actions you can turn to when things feel difficult.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.form("journal_profile_form"):
        st.text_input("Personal goal", key="journal_goal", max_chars=500)
        st.text_area("Why it matters", key="journal_why", max_chars=1000)
        for index in range(1, 4):
            st.text_input(
                f"Coping action {index}",
                key=f"journal_coping_{index}",
                max_chars=250,
            )
        save_profile = st.form_submit_button("Save plan")
    if save_profile:
        payload = {
            "goal": st.session_state.journal_goal or None,
            "why_it_matters": st.session_state.journal_why or None,
            "coping_actions": [
                st.session_state[f"journal_coping_{index}"]
                for index in range(1, 4)
                if st.session_state[f"journal_coping_{index}"].strip()
            ],
        }
        try:
            journal_write("patch", "/profile", headers, json=payload)
            st.success("Your plan was saved.")
        except requests.RequestException as error:
            st.error(journal_error_message(error))


def render_journal_entries(headers):
    st.markdown(
        """
        <div class="journal-panel-intro">
            <span class="eyebrow">A moment to reflect</span>
            <h2>Write a new entry</h2>
            <p>Choose only what fits. A category, an action, or a few private words is enough.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.form("journal_entry_form", clear_on_submit=True):
        category = st.selectbox(
            "What was happening?",
            options=[None, *JOURNAL_CATEGORIES],
            format_func=lambda value: journal_option_label(value, JOURNAL_CATEGORIES),
            key="journal_new_category",
        )
        action = st.selectbox(
            "What did you do?",
            options=[None, *JOURNAL_ACTIONS],
            format_func=lambda value: journal_option_label(value, JOURNAL_ACTIONS),
            key="journal_new_action",
        )
        note = st.text_area(
            "Private note (optional)",
            max_chars=2000,
            key="journal_new_note",
            help="Don't include website addresses, domains, DNS queries, or browsing history.",
        )
        save_entry = st.form_submit_button("Save entry")
    if save_entry:
        payload = {"category": category, "action": action, "note": note or None}
        if not any(payload.values()):
            st.error("Choose a category, an action, or write a note before saving.")
        else:
            try:
                journal_write("post", "/entries", headers, json=payload)
                refresh_journal_entries()
                st.session_state.journal_notice = True
                st.rerun()
            except requests.RequestException as error:
                st.error(journal_error_message(error))

    st.markdown(
        """
        <div class="journal-panel-intro journal-timeline-heading">
            <span class="eyebrow">Your timeline</span>
            <h2>Past entries</h2>
            <p>Return to earlier moments, notice patterns, or update what you wrote.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    entries = st.session_state.get("journal_entries", [])
    if not entries:
        st.markdown("**No journal entries yet.** When you save one, it will appear here newest first.")
    for entry in entries:
        entry_id = entry["id"]
        with st.expander(entry_heading(entry)):
            if entry.get("category"):
                st.write(f"**What was happening:** {JOURNAL_CATEGORIES.get(entry['category'], 'Other')} ")
            if entry.get("action"):
                st.write(f"**What you did:** {JOURNAL_ACTIONS.get(entry['action'], 'Other')}")
            if entry.get("note"):
                st.write(entry["note"])
            with st.form(f"journal_edit_{entry_id}"):
                edit_category = st.selectbox(
                    "Edit what was happening",
                    options=[None, *JOURNAL_CATEGORIES],
                    index=[None, *JOURNAL_CATEGORIES].index(entry.get("category")),
                    format_func=lambda value: journal_option_label(value, JOURNAL_CATEGORIES),
                    key=f"journal_edit_category_{entry_id}",
                )
                edit_action = st.selectbox(
                    "Edit what you did",
                    options=[None, *JOURNAL_ACTIONS],
                    index=[None, *JOURNAL_ACTIONS].index(entry.get("action")),
                    format_func=lambda value: journal_option_label(value, JOURNAL_ACTIONS),
                    key=f"journal_edit_action_{entry_id}",
                )
                edit_note = st.text_area(
                    "Edit private note",
                    value=entry.get("note") or "",
                    max_chars=2000,
                    key=f"journal_edit_note_{entry_id}",
                )
                save_changes = st.form_submit_button("Save changes")
            if save_changes:
                try:
                    journal_write(
                        "patch",
                        f"/entries/{entry_id}",
                        headers,
                        json={
                            "category": edit_category,
                            "action": edit_action,
                            "note": edit_note or None,
                        },
                    )
                    refresh_journal_entries()
                    st.session_state.journal_notice = True
                    st.rerun()
                except requests.RequestException as error:
                    st.error(journal_error_message(error))
            if st.button("Delete entry", key=f"journal_delete_{entry_id}"):
                try:
                    journal_write("delete", f"/entries/{entry_id}", headers)
                    refresh_journal_entries()
                    st.session_state.journal_notice = True
                    st.rerun()
                except requests.RequestException as error:
                    st.error(journal_error_message(error))

    next_cursor = st.session_state.get("journal_next_cursor")
    if next_cursor and st.button("Load more entries", key="journal_load_more"):
        try:
            next_page = journal_get(
                "/entries",
                headers,
                params={"limit": 50, "cursor": next_cursor},
            )
            st.session_state.journal_entries = [
                *entries,
                *(next_page.get("entries") or []),
            ]
            st.session_state.journal_next_cursor = next_page.get("next_cursor")
            st.rerun()
        except requests.RequestException as error:
            st.error(journal_error_message(error))


def render_progress_calendar(progress):
    today = datetime.date.today()
    month = calendar.Calendar(firstweekday=6).monthdayscalendar(today.year, today.month)
    by_day = {item["day"]: item for item in progress.get("days", [])}
    cells = []
    for week in month:
        for number in week:
            if not number:
                cells.append('<span class="progress-day empty" aria-hidden="true"></span>')
                continue
            day = datetime.date(today.year, today.month, number).isoformat()
            item = by_day.get(day)
            state = "unlogged"
            symbol = str(number)
            label = f"{day}: no check-in"
            if item and item.get("status") == "met_goal":
                state, symbol, label = "met", f"{number}<b>✓</b>", f"{day}: goal met"
            elif item and item.get("status") == "missed_goal":
                state, symbol, label = "missed", f"{number}<b>•</b>", f"{day}: fresh start"
            cells.append(f'<span class="progress-day {state}" aria-label="{label}">{symbol}</span>')
    st.markdown(
        f"""
        <div class="progress-calendar" aria-label="Daily progress calendar">
          <div class="progress-month">{today.strftime('%B %Y')}</div>
          <div class="progress-weekdays"><span>Sun</span><span>Mon</span><span>Tue</span><span>Wed</span><span>Thu</span><span>Fri</span><span>Sat</span></div>
          <div class="progress-days">{''.join(cells)}</div>
          <div class="progress-legend"><span><i class="met"></i>Goal met</span><span><i class="missed"></i>Fresh start</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_daily_progress(headers):
    st.markdown(
        """
        <div class="journal-panel-intro">
            <span class="eyebrow">One day at a time</span>
            <h2>Daily progress</h2>
            <p>Track the personal goal you chose and rebuild social confidence through small, respectful everyday interactions.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    try:
        progress = journal_get("/progress", headers)
    except requests.RequestException as error:
        st.error(journal_error_message(error))
        return

    current, best, social = st.columns(3)
    current.metric("Current streak", f"{progress.get('current_streak', 0)} days")
    best.metric("Best streak", f"{progress.get('best_streak', 0)} days")
    social.metric("Social steps", progress.get("social_steps", 0))
    render_progress_calendar(progress)

    today = datetime.date.today().isoformat()
    today_entry = next((item for item in progress.get("days", []) if item.get("day") == today), {})
    with st.form("daily_progress_form"):
        goal_type = st.selectbox(
            "Today's personal goal",
            options=list(DAILY_GOALS),
            index=list(DAILY_GOALS).index(today_entry.get("goal_type", "porn_and_masturbation_free")),
            format_func=DAILY_GOALS.get,
        )
        status = st.radio(
            "How did today go?",
            options=["met_goal", "missed_goal"],
            index=(
                ["met_goal", "missed_goal"].index(today_entry["status"])
                if today_entry.get("status") in ("met_goal", "missed_goal") else None
            ),
            format_func=lambda value: {
                "met_goal": "I met my goal today",
                "missed_goal": "I didn't meet it today",
            }[value],
        )
        social_goal = st.selectbox(
            "Optional social-confidence step",
            options=[None, *SOCIAL_GOALS],
            index=[None, *SOCIAL_GOALS].index(today_entry.get("social_goal")),
            format_func=lambda value: "Choose a small step" if value is None else SOCIAL_GOALS[value],
            help="The attempt counts. Be respectful, accept boundaries, and let the conversation end naturally.",
        )
        social_completed = st.checkbox(
            "I attempted this step",
            value=bool(today_entry.get("social_completed")),
        )
        save_progress = st.form_submit_button("Save today's progress")
    if save_progress:
        if status is None:
            st.error("Choose how today went before saving.")
        else:
            try:
                journal_write(
                    "put",
                    f"/progress/{today}",
                    headers,
                    json={
                        "goal_type": goal_type,
                        "status": status,
                        "social_goal": social_goal,
                        "social_completed": bool(social_goal and social_completed),
                    },
                )
                st.session_state.journal_notice = True
                st.rerun()
            except requests.RequestException as error:
                st.error(journal_error_message(error))

    if today_entry.get("status") == "missed_goal":
        st.info("One missed day doesn't erase your progress. Notice what happened, choose your next helpful action, and begin again today.")
    st.caption("This tracker supports goals you choose for yourself. It is not a diagnosis or a claim that masturbation causes social anxiety.")


def render_journal_data(headers):
    st.markdown(
        """
        <div class="journal-panel-intro">
            <span class="eyebrow">Privacy controls</span>
            <h2>Your journal data</h2>
            <p>Take a readable copy with you or permanently remove everything stored in your journal.</p>
        </div>
        <div class="journal-data-note"><strong>You stay in control.</strong><br>Exporting does not delete anything. Permanent deletion always requires confirmation.</div>
        """,
        unsafe_allow_html=True,
    )
    if st.button("Prepare JSON export", key="journal_prepare_export"):
        try:
            export = journal_get("/export", headers)
            st.session_state.journal_export = json.dumps(export, indent=2, ensure_ascii=False)
        except requests.RequestException as error:
            st.error(journal_error_message(error))
    if st.session_state.get("journal_export"):
        st.download_button(
            "Download journal export",
            data=st.session_state.journal_export,
            file_name="filtersight-journal-export.json",
            mime="application/json",
        )
    confirmed = st.checkbox(
        "I understand this permanently deletes my stored journal and plan.",
        key="journal_delete_all_confirmed",
    )
    if st.button(
        "Delete all journal data",
        type="secondary",
        disabled=not confirmed,
        key="journal_delete_all",
    ):
        try:
            journal_write("delete", "/data", headers)
            clear_journal_state()
            st.session_state.member_screen = "journal"
            st.session_state.journal_notice = True
            st.rerun()
        except requests.RequestException as error:
            st.error(journal_error_message(error))

    st.caption(
        "If your subscription has already ended and you cannot sign in, email support@filtersight.com from your subscription email to request deletion."
    )


def render_private_journal(headers):
    st.markdown(
        """
        <style>
        .stApp {
            min-height:100svh;
            background:
                radial-gradient(ellipse at 5% 95%, rgba(56,118,91,.16), transparent 34%),
                radial-gradient(ellipse at 95% 8%, rgba(240,125,99,.055), transparent 30%),
                linear-gradient(145deg,#061D18 0%,#0A2A22 58%,#071E19 100%) !important;
            color:#F7F5ED;
        }
        header[data-testid="stHeader"] { background:transparent !important; }
        .main .block-container {
            width:min(100% - 2rem,1040px);
            max-width:1040px;
            padding:1rem 0 4rem;
        }
        div[data-testid="stButton"] button,
        .stDownloadButton > button {
            min-height:46px;
            border:1px solid rgba(210,234,221,.28) !important;
            border-radius:10px !important;
            background:rgba(18,78,63,.78) !important;
            color:#FFF9EF !important;
            box-shadow:none !important;
            font-weight:750;
        }
        div[data-testid="stButton"] button:hover,
        .stDownloadButton > button:hover {
            border-color:rgba(232,245,237,.58) !important;
            transform:translateY(-1px);
        }
        div[data-testid="stButton"] button:focus-visible,
        .stDownloadButton > button:focus-visible {
            outline:3px solid #F8B3A4 !important;
            outline-offset:3px;
        }
        .journal-hero {
            margin:.25rem 0 .75rem;
            padding:clamp(1.35rem,4vw,2.25rem);
            border:1px solid rgba(203,229,216,.25);
            border-radius:20px;
            background:linear-gradient(135deg,rgba(246,251,247,.085),rgba(246,251,247,.03));
            box-shadow:0 16px 40px rgba(0,0,0,.15);
            backdrop-filter:blur(14px);
        }
        .journal-hero .journal-kicker {
            display:inline-flex;
            align-items:center;
            gap:.45rem;
            margin-bottom:.65rem;
            color:#A9D6C0;
            font-size:.72rem;
            font-weight:850;
            letter-spacing:.13em;
            text-transform:uppercase;
        }
        .journal-hero .journal-kicker::before {
            content:"";
            width:8px; height:8px;
            border-radius:50%;
            background:#F07D63;
            box-shadow:0 0 0 4px rgba(240,125,99,.12);
        }
        .journal-hero h1 {
            margin:0 0 .45rem;
            color:#FFF9EF !important;
            font-family:Georgia,"Times New Roman",serif;
            font-size:clamp(2.25rem,6vw,3.5rem);
            line-height:1;
            letter-spacing:-.04em;
        }
        .journal-hero p {
            max-width:650px;
            margin:0;
            color:#C5D7CE !important;
            font-size:.96rem;
            line-height:1.55;
        }
        .journal-trust {
            display:grid;
            grid-template-columns:repeat(3,1fr);
            gap:.65rem;
            margin:0 0 1rem;
        }
        .journal-trust-item {
            min-height:74px;
            padding:.8rem .9rem;
            border:1px solid rgba(203,229,216,.2);
            border-radius:13px;
            background:rgba(246,251,247,.055);
            color:#BFD2C8;
            font-size:.78rem;
            line-height:1.35;
        }
        .journal-trust-item strong {
            display:block;
            margin-bottom:.12rem;
            color:#FFF9EF;
            font-size:.84rem;
        }
        [data-baseweb="tab-list"] {
            gap:.35rem;
            padding:.35rem;
            border:1px solid rgba(203,229,216,.18);
            border-radius:14px;
            background:rgba(2,24,18,.5);
        }
        [data-baseweb="tab"] {
            min-height:46px;
            border-radius:10px;
            color:#BFD2C8 !important;
            font-weight:750;
        }
        [data-baseweb="tab"][aria-selected="true"] {
            background:rgba(38,103,83,.72);
            color:#FFF9EF !important;
        }
        [data-baseweb="tab-highlight"] { background:#F07D63 !important; }
        [data-baseweb="tab-panel"] {
            padding-top:1rem;
        }
        .journal-panel-intro {
            margin:.25rem 0 .85rem;
            padding:.2rem .1rem;
        }
        .journal-panel-intro .eyebrow {
            color:#9FCDB7;
            font-size:.7rem;
            font-weight:850;
            letter-spacing:.12em;
            text-transform:uppercase;
        }
        .journal-panel-intro h2 {
            margin:.15rem 0 .25rem;
            color:#FFF9EF !important;
            font-family:Georgia,"Times New Roman",serif;
            font-size:clamp(1.65rem,4vw,2.15rem);
            letter-spacing:-.025em;
        }
        .journal-panel-intro p {
            max-width:650px;
            margin:0;
            color:#BFD2C8 !important;
            line-height:1.5;
        }
        .journal-timeline-heading {
            margin-top:1.7rem;
            padding-top:1.35rem;
            border-top:1px solid rgba(203,229,216,.16);
        }
        [data-testid="stForm"] {
            padding:1rem 1.1rem 1.15rem;
            border:1px solid rgba(203,229,216,.22);
            border-radius:16px;
            background:rgba(246,251,247,.06);
            box-shadow:0 10px 28px rgba(0,0,0,.1);
        }
        [data-testid="stWidgetLabel"] p,
        .stCheckbox label p { color:#DCE8E2 !important; }
        .stTextInput input,
        .stTextArea textarea,
        div[data-baseweb="select"] > div {
            border-color:#B9CBC2 !important;
            background:#F7F6F0 !important;
            color:#152A23 !important;
            -webkit-text-fill-color:#152A23 !important;
        }
        [data-testid="stExpander"] {
            margin-bottom:.55rem;
            border:1px solid rgba(203,229,216,.2) !important;
            border-radius:13px !important;
            background:rgba(246,251,247,.055) !important;
            box-shadow:none !important;
        }
        [data-testid="stExpander"] summary,
        [data-testid="stExpander"] p { color:#DCE8E2 !important; }
        .journal-data-note {
            margin:.25rem 0 1rem;
            padding:.9rem 1rem;
            border:1px solid rgba(240,125,99,.28);
            border-left:4px solid #F07D63;
            border-radius:10px;
            background:rgba(240,125,99,.075);
            color:#E5D7D1;
            font-size:.86rem;
            line-height:1.5;
        }
        .progress-calendar {
            margin:.75rem 0 1.25rem;
            padding:1rem;
            border:1px solid rgba(203,229,216,.2);
            border-radius:16px;
            background:rgba(246,251,247,.055);
        }
        .progress-month { margin-bottom:.75rem; color:#FFF9EF; font:700 1.1rem Georgia,serif; }
        .progress-weekdays,.progress-days { display:grid; grid-template-columns:repeat(7,1fr); gap:.35rem; }
        .progress-weekdays span { padding:.2rem; color:#9FCDB7; font-size:.65rem; text-align:center; text-transform:uppercase; }
        .progress-day { position:relative; min-height:44px; display:grid; place-items:center; border-radius:10px; background:rgba(255,255,255,.05); color:#C9D9D1; font-size:.78rem; }
        .progress-day b { position:absolute; right:4px; bottom:2px; font-size:.68rem; }
        .progress-day.met { background:rgba(93,190,137,.2); color:#EDFFF4; box-shadow:inset 0 0 0 1px rgba(126,220,165,.3); }
        .progress-day.missed { background:rgba(240,125,99,.14); color:#FFE9E3; box-shadow:inset 0 0 0 1px rgba(240,125,99,.25); }
        .progress-day.empty { visibility:hidden; }
        .progress-legend { display:flex; gap:1rem; margin-top:.7rem; color:#AFC6BA; font-size:.72rem; }
        .progress-legend span { display:flex; align-items:center; gap:.35rem; }
        .progress-legend i { width:9px; height:9px; border-radius:50%; }
        .progress-legend i.met { background:#5DBE89; }
        .progress-legend i.missed { background:#F07D63; }
        [data-testid="stCaptionContainer"] p { color:#9EB7AB !important; }
        @media(max-width:620px) {
            .main .block-container { width:calc(100% - 1.15rem); padding-top:.55rem; }
            .journal-hero { padding:1.25rem 1.05rem; border-radius:16px; }
            .journal-hero h1 { font-size:2.35rem; }
            .journal-trust { grid-template-columns:1fr; gap:.42rem; }
            .journal-trust-item { min-height:0; padding:.65rem .75rem; }
            [data-baseweb="tab-list"] { gap:.15rem; }
            [data-baseweb="tab"] { padding-inline:.6rem; font-size:.78rem; }
            [data-testid="stForm"] { padding:.85rem; }
        }
        @media(prefers-reduced-motion:reduce) {
            *, *::before, *::after { transition:none !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    if st.button("← Back to dashboard", key="journal_back"):
        clear_journal_state()
        go_to_member_screen()

    st.markdown(
        """
        <section class="journal-hero" aria-labelledby="journal-title">
            <span class="journal-kicker">Your protected space</span>
            <h1 id="journal-title">Private journal</h1>
            <p>A quiet place to remember what matters, make a plan, and notice your progress—at your own pace.</p>
        </section>
        <div class="journal-trust" aria-label="Journal privacy information">
            <div class="journal-trust-item"><strong>Encrypted</strong>Your writing is encrypted before storage.</div>
            <div class="journal-trust-item"><strong>Kept separate</strong>Nothing is sent to the AI companion automatically.</div>
            <div class="journal-trust-item"><strong>Always yours</strong>Edit, export, or delete your journal anytime.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    try:
        if not st.session_state.get("journal_profile_loaded"):
            profile = journal_get("/profile", headers)
            st.session_state.journal_goal = profile.get("goal") or ""
            st.session_state.journal_why = profile.get("why_it_matters") or ""
            actions = list(profile.get("coping_actions") or [])[:3]
            actions += [""] * (3 - len(actions))
            for index, action in enumerate(actions, start=1):
                st.session_state[f"journal_coping_{index}"] = action
            st.session_state.journal_profile_loaded = True
        if not st.session_state.get("journal_entries_loaded"):
            entries_payload = journal_get("/entries", headers, params={"limit": 50})
            st.session_state.journal_entries = entries_payload.get("entries") or []
            st.session_state.journal_next_cursor = entries_payload.get("next_cursor")
            st.session_state.journal_entries_loaded = True
    except requests.RequestException as error:
        st.error(journal_error_message(error))
        return

    if st.session_state.pop("journal_notice", None):
        st.success("Your journal changes were saved.")

    progress_tab, plan_tab, entries_tab, data_tab = st.tabs(
        ["Daily progress", "My plan", "Journal entries", "Data & privacy"]
    )
    with progress_tab:
        render_daily_progress(headers)
    with plan_tab:
        render_journal_plan(headers)
    with entries_tab:
        render_journal_entries(headers)
    with data_tab:
        render_journal_data(headers)


def render_help_action():
    if st.button("I need help now", key=f"help_{st.session_state.get('checkin_screen', 'dashboard')}"):
        go_to_checkin("emergency")


def render_numbered_steps(steps):
    for number, step in enumerate(steps, start=1):
        st.write(f"{number}. {step}")


def render_companion(headers):
    if st.button("← Back to dashboard", key="companion_back"):
        clear_checkin_state()
        st.rerun()

    st.markdown(
        """
        <style>
        .stApp {
            min-height:100svh;
            background:
                radial-gradient(circle at 18% 24%, rgba(104,165,137,.08) 0 1px, transparent 2px),
                radial-gradient(circle at 78% 64%, rgba(240,138,114,.055) 0 1px, transparent 2px),
                linear-gradient(145deg,#071E18 0%,#0A2A21 54%,#061A16 100%) !important;
            background-size:42px 42px, 54px 54px, auto;
        }
        header[data-testid="stHeader"] {
            background:transparent !important;
        }
        .main .block-container {
            width:min(100% - 2rem, 1080px);
            max-width:1080px;
            min-height:calc(100svh - 1rem);
            padding:clamp(.75rem,2vw,1.5rem) 0 8.75rem;
        }
        [data-testid="stMainBlockContainer"] > div:first-child {
            gap:.65rem;
        }
        div[data-testid="stButton"] button {
            min-height:44px;
            border:1px solid rgba(236,248,240,.25) !important;
            border-radius:999px !important;
            background:rgba(244,249,244,.08) !important;
            color:#F8F5EC !important;
            box-shadow:none !important;
            font-weight:700;
        }
        div[data-testid="stButton"] button:hover {
            border-color:rgba(236,248,240,.52) !important;
            background:rgba(244,249,244,.14) !important;
        }
        div[data-testid="stButton"] button:focus-visible {
            outline:3px solid #F08A72 !important;
            outline-offset:3px;
        }
        .companion-header {
            display:flex;
            align-items:center;
            gap:1rem;
            margin:.1rem 0 clamp(1.5rem,5vh,3.75rem);
            padding:1.1rem clamp(1rem,3vw,1.75rem);
            border:1px solid rgba(183,220,198,.28);
            border-radius:22px;
            background:linear-gradient(110deg,rgba(11,45,36,.96),rgba(20,73,58,.94));
            box-shadow:0 18px 48px rgba(0,0,0,.22);
            backdrop-filter:blur(16px);
        }
        .companion-avatar {
            position:relative;
            flex:0 0 auto;
            width:54px; height:54px;
            display:grid; place-items:center;
            border-radius:17px;
            background:#F08A72;
            color:#102821;
            font-family:Georgia,"Times New Roman",serif;
            font-size:1.2rem;
            font-weight:800;
            box-shadow:0 0 0 5px rgba(240,138,114,.13);
        }
        .companion-avatar::after {
            content:"";
            position:absolute;
            right:-2px; bottom:-2px;
            width:12px; height:12px;
            border:3px solid #183D32;
            border-radius:50%;
            background:#8DDBAD;
        }
        .companion-heading { min-width:0; flex:1; }
        .companion-heading h1 {
            margin:0 0 .18rem;
            color:#FFF9EF !important;
            font-family:Georgia,"Times New Roman",serif;
            font-size:clamp(1.35rem,3vw,2rem);
            letter-spacing:-.02em;
        }
        .companion-heading p {
            margin:0;
            color:#BFD9CD !important;
            font-size:.87rem;
        }
        .companion-private {
            flex:0 0 auto;
            display:inline-flex;
            align-items:center;
            gap:.45rem;
            padding:.55rem .8rem;
            border:1px solid rgba(190,226,206,.3);
            border-radius:999px;
            color:#DDF2E6;
            font-size:.75rem;
            font-weight:700;
        }
        .companion-private::before {
            content:"";
            width:8px; height:8px;
            border-radius:50%;
            background:#70DA92;
            box-shadow:0 0 0 3px rgba(112,218,146,.12);
        }
        .companion-intro {
            margin:0 0 .45rem;
            padding:.8rem 1rem;
            border:1px solid rgba(211,235,221,.18);
            border-radius:14px;
            background:rgba(237,247,240,.08);
            color:#C8DBD2;
            font-size:.84rem;
            line-height:1.5;
        }
        .companion-intro strong { color:#FFF9EF; }
        [data-testid="stChatMessage"] {
            align-items:flex-end;
            gap:.75rem;
            padding:.55rem 0;
            background:transparent !important;
        }
        [data-testid="stChatMessageContent"] {
            max-width:min(76%,720px);
            padding:.9rem 1.15rem;
            border:1px solid rgba(244,241,230,.78);
            border-radius:24px 24px 24px 7px;
            background:linear-gradient(145deg,#FBFAF5,#F2F0E8);
            box-shadow:0 10px 28px rgba(0,0,0,.14);
        }
        [data-testid="stChatMessageContent"] p {
            margin:0;
            color:#1B2D26 !important;
            line-height:1.52;
        }
        [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
            flex-direction:row-reverse;
        }
        [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"] {
            border-color:#2C715E;
            border-radius:24px 24px 7px 24px;
            background:linear-gradient(145deg,#2D725F,#205B4C);
            box-shadow:0 10px 26px rgba(0,0,0,.17);
        }
        [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"] p {
            color:#FFFFFF !important;
        }
        [data-testid="stChatMessageAvatarAssistant"],
        [data-testid="stChatMessageAvatarUser"] {
            width:42px;
            height:42px;
            border-radius:50%;
        }
        [data-testid="stChatMessageAvatarAssistant"] {
            background:#F07D63;
            box-shadow:0 0 0 4px rgba(240,125,99,.13);
        }
        [data-testid="stChatMessageAvatarUser"] { display:none; }
        [data-testid="stChatInput"],
        [data-testid="stChatInput"] > div,
        [data-testid="stChatInput"] div[data-baseweb="textarea"] {
            border-color:#B8C9C0 !important;
            background:#FFFFFF !important;
        }
        [data-testid="stChatInput"] {
            overflow:hidden;
            border:1px solid rgba(255,255,255,.78) !important;
            border-radius:26px !important;
            box-shadow:0 16px 42px rgba(0,0,0,.28);
        }
        [data-testid="stChatInput"] textarea {
            background:#FFFFFF !important;
            color:#1B2D26 !important;
            -webkit-text-fill-color:#1B2D26 !important;
            caret-color:#1F5A49;
        }
        [data-testid="stChatInput"] textarea::placeholder {
            color:#68766F !important;
            -webkit-text-fill-color:#68766F !important;
            opacity:1;
        }
        [data-testid="stChatInput"] button {
            width:42px;
            height:42px;
            margin:.3rem;
            border-radius:50% !important;
            background:#F07D63 !important;
            color:#FFFFFF !important;
        }
        [data-testid="stBottom"],
        [data-testid="stBottomBlockContainer"],
        .stBottomBlockContainer {
            width:min(100% - 2rem,1080px) !important;
            margin-inline:auto !important;
            padding:.8rem 0 max(1rem,env(safe-area-inset-bottom)) !important;
            background:linear-gradient(180deg,rgba(6,26,22,0),#061A16 34%) !important;
        }
        .companion-controls {
            margin:1.6rem 0 .35rem;
            padding-top:1rem;
            border-top:1px solid rgba(220,235,226,.18);
            color:#AFC5BA;
            font-size:.82rem;
        }
        [data-testid="stExpander"] {
            border:1px solid rgba(220,235,226,.2) !important;
            border-radius:16px !important;
            background:rgba(244,249,244,.06) !important;
            color:#F8F5EC !important;
        }
        [data-testid="stExpander"] summary,
        [data-testid="stExpander"] p {
            color:#DDE9E2 !important;
        }
        @media (max-width:480px) {
            .main .block-container {
                width:calc(100% - 1.25rem);
                padding-top:.45rem;
                padding-bottom:8rem;
            }
            .companion-header {
                gap:.75rem;
                margin-bottom:clamp(1.3rem,6vh,3rem);
                padding:.9rem;
                border-radius:17px;
            }
            .companion-avatar { width:46px; height:46px; border-radius:14px; }
            .companion-heading h1 { font-size:1.25rem; }
            .companion-heading p { font-size:.76rem; }
            .companion-private { padding:.5rem; font-size:0; }
            .companion-private::before { width:10px; height:10px; }
            .companion-intro { font-size:.78rem; }
            [data-testid="stChatMessageContent"] { max-width:86%; padding:.78rem 1rem; }
            [data-testid="stChatMessageAvatarAssistant"] { width:38px; height:38px; }
            .stBottomBlockContainer { width:calc(100% - 1.25rem) !important; }
        }
        @media (min-width:900px) and (min-height:700px) {
            [data-testid="stChatMessage"] { padding:.75rem 1.25rem; }
        }
        @media (prefers-reduced-motion:reduce) {
            *, *::before, *::after { scroll-behavior:auto !important; transition:none !important; }
        }
        </style>
        <section class="companion-header" aria-labelledby="companion-title">
            <div class="companion-avatar" aria-hidden="true">FS</div>
            <div class="companion-heading">
                <h1 id="companion-title">FilterSight Companion</h1>
                <p>Online · Here with you now</p>
            </div>
            <span class="companion-private">Private session</span>
        </section>
        """,
        unsafe_allow_html=True,
    )
    help_space, help_action = st.columns([1, .55])
    with help_action:
        if st.button("I need help now", key="companion_help", type="primary", use_container_width=True):
            go_to_checkin("emergency")
    try:
        usage_response = requests.get(
            f"{BACKEND_URL}/member/chat/usage", headers=headers, timeout=15
        )
        usage_response.raise_for_status()
        chat_usage = usage_response.json()
        st.caption(
            f"{chat_usage.get('remaining', 0)} of {chat_usage.get('limit', 20)} companion messages remaining today · Resets daily at 12:00 AM UTC"
        )
    except requests.RequestException:
        chat_usage = None
    if "member_chat_history" not in st.session_state:
        st.session_state.member_chat_history = [{"role": "assistant", "content": "I'm here. What's on your mind?"}]
    for item in st.session_state.member_chat_history:
        with st.chat_message(item["role"]):
            st.write(item["content"])
    prompt = st.chat_input("Message your companion…")
    if prompt:
        history = st.session_state.member_chat_history[-20:]
        st.session_state.member_chat_history.append({"role": "user", "content": prompt})
        try:
            response = requests.post(
                f"{BACKEND_URL}/member/chat", headers=headers,
                json={"message": prompt, "history": history}, timeout=30,
            )
            if response.status_code == 429:
                reply = "You've reached today's 20-message companion limit. You can chat again tomorrow."
            else:
                response.raise_for_status()
                reply = response.json().get("reply", "I'm here. Can you tell me a bit more about what's going on?")
        except requests.RequestException:
            reply = "I couldn't connect just now. Please try again in a moment."
        st.session_state.member_chat_history.append({"role": "assistant", "content": reply})
        st.rerun()
    with st.expander("Privacy & session controls"):
        st.markdown(
            "**A calm place to talk.** No judgment and no pressure—share as much or as little as you want. "
            "Your message and recent conversation context are sent to Anthropic for a reply. "
            "FilterSight does not save this conversation in its database."
        )
        st.markdown(
            '<div class="companion-controls">Ending this chat clears the conversation from your current session.</div>',
            unsafe_allow_html=True,
        )
        if st.button("End private chat", use_container_width=True):
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
        st.markdown(
            """
            <section class="checkin-hero" aria-labelledby="checkin-title">
                <span class="checkin-kicker">Private check-in</span>
                <h1 id="checkin-title">Let’s pause for a moment.</h1>
                <p>Tell us what feels closest right now. This stays in your current session and helps us guide you to the right kind of support.</p>
            </section>
            """,
            unsafe_allow_html=True,
        )
        render_help_action()
        st.markdown(
            """
            <div class="checkin-guidance">
                <span class="checkin-guidance-icon" aria-hidden="true">✓</span>
                <div><strong>There’s no wrong answer.</strong><br>Choose the one that feels most true right now.</div>
            </div>
            <div class="checkin-section-label">What’s happening right now?</div>
            """,
            unsafe_allow_html=True,
        )
        choice = st.radio(
            "What's happening right now?",
            CHECKIN_OPTIONS,
            index=None,
            key="checkin_choice",
            label_visibility="collapsed",
        )
        if st.button("Continue", key="checkin_continue", type="primary", use_container_width=True):
            if not choice:
                st.error("Pick the option that's closest.")
            else:
                go_to_checkin(route_checkin(choice))
        if st.button("Not right now", key="checkin_done", use_container_width=True):
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


def sign_out_member(headers):
    try:
        requests.post(f"{BACKEND_URL}/member/logout", headers=headers, timeout=10)
    except requests.RequestException:
        pass
    st.session_state.pop("member_access_token", None)
    st.session_state.pop("member_chat_history", None)
    st.session_state.pop("member_screen", None)
    clear_checkin_state()
    clear_journal_state()
    st.rerun()


def render_dashboard_home(profile, headers):
    st.markdown(
        """
        <style>
        .stApp {
            min-height:100svh;
            background:
                radial-gradient(ellipse at 3% 98%, rgba(53,112,88,.18), transparent 32%),
                radial-gradient(ellipse at 96% 12%, rgba(32,91,72,.14), transparent 34%),
                linear-gradient(145deg,#061D18 0%,#0A2A22 58%,#071E19 100%) !important;
            color:#F7F5ED;
        }
        header[data-testid="stHeader"] { background:transparent !important; }
        .main .block-container {
            width:min(100% - 2rem,1040px);
            max-width:1040px;
            padding:1rem 0 3.5rem;
        }
        .dashboard-brandbar {
            display:flex;
            align-items:center;
            justify-content:space-between;
            gap:1rem;
            margin-bottom:1.25rem;
            padding:.72rem .9rem;
            border:1px solid rgba(213,235,224,.16);
            border-radius:16px;
            background:rgba(5,31,24,.64);
            box-shadow:0 12px 34px rgba(0,0,0,.16);
            backdrop-filter:blur(14px);
        }
        .dashboard-brand {
            display:flex;
            align-items:center;
            gap:.72rem;
            color:#FFF9EF;
            font-size:1.12rem;
            font-weight:800;
        }
        .dashboard-mark {
            width:42px; height:42px;
            display:grid; place-items:center;
            border-radius:50%;
            background:#F07D63;
            color:#FFF9EF;
            font-family:Georgia,"Times New Roman",serif;
            font-size:1.05rem;
            box-shadow:0 0 0 4px rgba(240,125,99,.1);
        }
        .dashboard-email {
            max-width:50%;
            overflow:hidden;
            color:#D8E6DF;
            font-size:.88rem;
            text-overflow:ellipsis;
            white-space:nowrap;
        }
        [data-testid="stVerticalBlockBorderWrapper"] {
            margin-bottom:.4rem;
            border:1px solid rgba(203,229,216,.24) !important;
            border-radius:18px !important;
            background:linear-gradient(135deg,rgba(246,251,247,.07),rgba(246,251,247,.025)) !important;
            box-shadow:0 12px 34px rgba(0,0,0,.13);
            backdrop-filter:blur(12px);
        }
        [data-testid="stVerticalBlockBorderWrapper"] > div {
            padding:clamp(.35rem,1vw,.8rem) clamp(.25rem,1.5vw,.85rem) !important;
        }
        .dashboard-hero h1 {
            margin:.05rem 0 .85rem;
            color:#FFF9EF !important;
            font-family:Georgia,"Times New Roman",serif;
            font-size:clamp(2.1rem,5.4vw,3.25rem);
            line-height:1.04;
            letter-spacing:-.035em;
        }
        .dashboard-status {
            display:flex;
            align-items:center;
            gap:.65rem;
            padding:.85rem 1rem;
            border-radius:13px;
            background:linear-gradient(105deg,#BCE3CE,#A8D7BF);
            color:#112A22;
            font-size:.92rem;
        }
        .dashboard-status::before {
            content:"✓";
            width:24px; height:24px;
            display:grid; place-items:center;
            flex:0 0 auto;
            border:1.5px solid #173C31;
            border-radius:50%;
            font-weight:900;
        }
        .dashboard-card-heading {
            margin:.1rem 0 .2rem;
            color:#FFF9EF;
            font-family:Georgia,"Times New Roman",serif;
            font-size:clamp(1.55rem,3.5vw,2rem);
            line-height:1.15;
        }
        .dashboard-card-copy {
            max-width:610px;
            margin:0 0 .45rem;
            color:#C2D4CB;
            font-size:.94rem;
            line-height:1.52;
        }
        .dashboard-lock-note {
            display:flex;
            align-items:center;
            gap:.42rem;
            color:#9FC8B5;
            font-size:.76rem;
        }
        .dashboard-lock-note::before { content:"▣"; }
        div[data-testid="stButton"] button {
            min-height:48px;
            border:1px solid rgba(210,234,221,.28) !important;
            border-radius:10px !important;
            background:rgba(18,78,63,.72) !important;
            color:#FFF9EF !important;
            box-shadow:none !important;
            font-weight:750;
        }
        div[data-testid="stButton"] button[kind="primary"] {
            border-color:#F07D63 !important;
            background:linear-gradient(135deg,#F2715F,#E55E51) !important;
            box-shadow:0 8px 20px rgba(226,91,77,.18) !important;
        }
        div[data-testid="stButton"] button:hover {
            border-color:rgba(232,245,237,.56) !important;
            transform:translateY(-1px);
        }
        div[data-testid="stButton"] button:focus-visible {
            outline:3px solid #F8B3A4 !important;
            outline-offset:3px;
        }
        [data-testid="stExpander"] {
            border:1px solid rgba(210,234,221,.2) !important;
            border-radius:12px !important;
            background:rgba(3,25,19,.28) !important;
            box-shadow:none !important;
        }
        [data-testid="stExpander"] summary,
        [data-testid="stExpander"] p { color:#D7E5DE !important; }
        @media(max-width:620px) {
            .main .block-container { width:calc(100% - 1.15rem); padding-top:.55rem; }
            .dashboard-brandbar { margin-bottom:.8rem; padding:.62rem .7rem; }
            .dashboard-mark { width:38px; height:38px; }
            .dashboard-email { max-width:48%; font-size:.78rem; }
            [data-testid="stVerticalBlockBorderWrapper"] { border-radius:15px !important; }
            .dashboard-hero h1 { font-size:2.25rem; }
            .dashboard-card-copy { font-size:.88rem; }
            div[data-testid="stButton"] button { min-height:46px; }
        }
        @media(prefers-reduced-motion:reduce) {
            *, *::before, *::after { transition:none !important; }
        }
        </style>
        <nav class="dashboard-brandbar" aria-label="FilterSight account">
            <div class="dashboard-brand"><span class="dashboard-mark" aria-hidden="true">FS</span>FilterSight</div>
            <div class="dashboard-email">{email}</div>
        </nav>
        """.replace("{email}", escape(profile["email"])),
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        st.markdown(
            f"""
            <section class="dashboard-hero" aria-labelledby="dashboard-title">
                <h1 id="dashboard-title">Your FilterSight dashboard</h1>
                <div class="dashboard-status">Signed in as {escape(profile['email'])} · FilterSight</div>
            </section>
            """,
            unsafe_allow_html=True,
        )
        if profile["cancel_at_period_end"]:
            st.info("Your subscription is set to end at the close of the current billing period. No cancellation fee is charged.")
        else:
            with st.expander("Manage subscription"):
                st.write("Your plan stays active through the current billing period. Canceling has no fee.")
                if st.button("Cancel my subscription", key="dashboard_cancel"):
                    try:
                        response = requests.post(f"{BACKEND_URL}/member/cancel", headers=headers, timeout=15)
                        response.raise_for_status()
                        st.success("Cancellation scheduled for the end of your current billing period. No fee was charged.")
                        st.rerun()
                    except requests.RequestException:
                        st.error("We couldn't schedule cancellation right now. Please try again.")

    with st.container(border=True):
        st.markdown(
            '<h2 class="dashboard-card-heading">Support in the moment</h2>'
            '<p class="dashboard-card-copy">Quick ways to pause, reflect, and feel supported right now.</p>',
            unsafe_allow_html=True,
        )
        checkin_column, chat_column, spacer = st.columns([1, 1, 1.8])
        with checkin_column:
            if st.button("Check in", type="primary", use_container_width=True):
                go_to_checkin("checkin")
        with chat_column:
            if st.button("Talk it through", use_container_width=True):
                go_to_checkin("chat")

    with st.container(border=True):
        st.markdown(
            '<h2 class="dashboard-card-heading">Private journal</h2>'
            '<p class="dashboard-card-copy">Your encrypted space for goals, reflections, and what matters most.</p>'
            '<p class="dashboard-lock-note">Only you can read it.</p>',
            unsafe_allow_html=True,
        )
        journal_column, spacer = st.columns([1.25, 2.75])
        with journal_column:
            if st.button("Open private journal", use_container_width=True):
                go_to_member_screen("journal")
        if st.button("Sign out", key="dashboard_sign_out"):
            sign_out_member(headers)


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

    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(f"{BACKEND_URL}/member/profile", headers=headers, timeout=15)
        response.raise_for_status()
        profile = response.json()
    except requests.RequestException:
        st.session_state.pop("member_access_token", None)
        st.error("Your sign-in expired or your subscription could not be verified. Request a new link below.")
        st.rerun()

    # The companion is intentionally an immersive, distraction-free screen.
    # Account controls remain one step away via the persistent back button.
    if st.session_state.get("checkin_screen") == "chat":
        render_companion(headers)
        return

    if not st.session_state.get("checkin_screen") and st.session_state.get("member_screen") != "journal":
        render_dashboard_home(profile, headers)
        return

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
    elif st.session_state.get("member_screen") == "journal":
        render_private_journal(headers)
    else:
        st.subheader("Support in the moment")
        st.caption("A calm, private place to pause, reset, or talk things through.")
        if st.button("Check in", type="primary"):
            go_to_checkin("checkin")
        if st.button("Talk it through"):
            go_to_checkin("chat")
        st.divider()
        st.subheader("Private journal")
        st.caption("Keep an encrypted goal, coping plan, and private notes you can return to.")
        if st.button("Open private journal"):
            go_to_member_screen("journal")

    if st.button("Sign out"):
        sign_out_member(headers)

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
            try:
                session = stripe.checkout.Session.create(
                    mode="subscription",
                    customer_email=normalized_email,
                    line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
                    success_url=f"{APP_BASE_URL}/?session_id={{CHECKOUT_SESSION_ID}}",
                    cancel_url=APP_BASE_URL,
                    metadata={"plan": PLAN_ID},
                    subscription_data={"metadata": {"plan": PLAN_ID}},
                )
            except Exception as e:
                logger.error("checkout_session_creation_failed error=%s", type(e).__name__)
                st.error(
                    "We couldn't start checkout right now. Please try again in a moment "
                    "or contact support@filtersight.com."
                )
            else:
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
        logger.error("payment_verification_failed error=%s", type(e).__name__)
        paid = False
        customer_email = None
        plan_id = None

    if not paid:
        st.error("We couldn't verify this payment. If you were just charged, contact support.")
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
