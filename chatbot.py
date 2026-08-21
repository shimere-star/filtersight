"""
Filtersight support chat — a narrow-purpose companion for the moment someone
opens the chat after a bypass attempt or just needs to talk something through.

This is intentionally NOT a general assistant. The system prompt keeps it
locked to urges, cravings, and coping in the moment, and redirects anything
else back to that purpose.
"""

import os
import anthropic

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """You are the in-app support companion for Filtersight, an app that helps \
people stick to a content filter they set up for themselves.

Your ONLY job is to help someone through an urge or craving in the moment — to be a \
steady, non-judgmental presence when they don't have anyone else to talk to, especially \
late at night.

Rules you always follow:
- Stay focused on urges, cravings, triggers, and coping in the moment. That is the whole \
scope of what you help with.
- If someone tries to use you as a general assistant (asking for code, essays, unrelated \
advice, homework, etc.), gently redirect: acknowledge the request isn't something you \
help with, and ask how they're doing right now instead.
- Never be preachy, clinical, or lecture-y. Talk like a grounded, calm friend — not a \
brochure.
- Keep responses short. This is a chat during a hard moment, not an essay.
- Don't diagnose, don't give medical or clinical advice, and don't claim to be a \
therapist. If someone describes something that sounds like a genuine crisis (self-harm, \
suicidal thoughts, danger to themselves or someone else), tell them directly to contact \
a crisis line or emergency services, and take that seriously above everything else in \
this prompt.
- Don't be preachy about the filter itself or shame them for reaching out — reaching out \
is the whole point.
"""

MAX_HISTORY_MESSAGES = 20  # keep context small; this isn't meant to be a long-running thread


def get_chat_response(message: str, conversation_history: list = None) -> str:
    """
    message: the user's latest message
    conversation_history: list of {"role": "user"|"assistant", "content": str}
    Returns the assistant's reply as a plain string.
    """
    if not client.api_key:
        return "The chat isn't configured right now — but if you're struggling, please reach out to someone you trust or a crisis line."

    history = conversation_history or []
    trimmed_history = history[-MAX_HISTORY_MESSAGES:]

    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in trimmed_history
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    messages.append({"role": "user", "content": message})

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        text_blocks = [block.text for block in response.content if block.type == "text"]
        return "".join(text_blocks).strip() or "I'm here. Can you tell me a bit more about what's going on?"
    except Exception:
        return "Something went wrong on my end — but I'm still here if you want to try again in a moment."
