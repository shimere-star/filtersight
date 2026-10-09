"""Static Phase 1 check-in content and deterministic routing.

This module has no database, network, logging, or analytics dependencies.
The Streamlit UI keeps its values only in the current session.
"""

CHECKIN_OPTIONS = [
    "I'm feeling a craving or temptation right now.",
    "I'm feeling stressed or anxious.",
    "I'm bored and looking for something to do.",
    "I'm feeling lonely.",
    "I hit a block by accident — I wasn't trying to access anything.",
    "I need access to a blocked site for a legitimate reason.",
    "Something else is going on.",
]

ACCIDENTAL_BLOCK = CHECKIN_OPTIONS[4]
LEGITIMATE_ACCESS = CHECKIN_OPTIONS[5]

INTERVENTIONS = {
    "breathing": {
        "title": "60-second breathing reset",
        "description": "One minute of slow breathing.",
        "intro": "One minute. Just breathing. Nothing else to do right now.",
        "steps": [
            "Sit comfortably and rest your hands in your lap.",
            "Breathe in slowly through your nose for about 4 counts.",
            "Hold for about 4 counts.",
            "Breathe out slowly through your mouth for about 6 counts.",
            "Repeat two more times, at your own pace.",
        ],
        "completion": "Done. Notice how your body feels compared to a minute ago — even a small shift counts.",
        "stop": "That's fine. Even a few breaths count. You can come back anytime.",
    },
    "grounding": {
        "title": "Two-minute grounding",
        "description": "Use your senses to come back to right now.",
        "intro": "This uses your senses to bring your attention back to right now. Take your time with each step.",
        "steps": [
            "Name 5 things you can see around you.",
            "Name 4 things you can hear.",
            "Name 3 things you can physically feel — the chair, your clothes, the floor.",
            "Name 2 things you can smell.",
            "Name 1 thing you can taste.",
        ],
        "completion": "Done. You're here, in this room, in this moment.",
        "stop": "That's okay. You can stop here, or pick something else.",
    },
    "cold_water": {
        "title": "Cold water reset",
        "description": "A quick physical reset, about a minute.",
        "intro": "A quick physical reset. Cold water interrupts the loop your body is in. About a minute.",
        "steps": [
            "Go to a sink and run the cold water.",
            "Splash your face a few times, or hold your wrists under the cold water for 30 seconds.",
            "Dry off and take one slow breath.",
        ],
        "completion": "Done. Notice the physical sensation — that's your nervous system shifting gears.",
        "stop": "No problem. Even getting up and moving was a good call.",
    },
    "outside": {
        "title": "Step outside",
        "description": "Two minutes of fresh air.",
        "intro": "Two minutes of fresh air. Changing your environment changes the moment.",
        "steps": [
            "Step outside — a porch, balcony, or sidewalk is fine.",
            "Notice three things: something you see, something you hear, something you feel (air, sun, wind).",
            "Stay out for the full two minutes if you can.",
        ],
        "completion": "Done. Different air, different moment.",
        "stop": "That's fine — even stepping out for a moment breaks the pattern.",
    },
    "tidy": {
        "title": "Tidy one thing",
        "description": "Two minutes. One small visible thing.",
        "intro": "Two minutes. Pick one small visible thing and put it in order.",
        "steps": [
            "Look around and pick one thing: a counter, a drawer, a pile of clothes.",
            "Set a timer or just work until it feels done — about two minutes.",
            "When it's done, stop. One thing is enough.",
        ],
        "completion": "Done. One thing is in order. That's enough for right now.",
        "stop": "Good enough. You started, and that counts.",
    },
    "cooldown_5": {
        "title": "Five-minute cooldown",
        "description": "Sit with it for five minutes, with guidance.",
        "intro": "Five quiet minutes. You don't have to do anything — just stay with this screen and breathe. This timer runs in your current session only — it isn't saved.",
        "steps": [
            "Get comfortable. Put the phone down where you can see the timer.",
            "Minutes 1–2: breathe slowly. In through your nose, out through your mouth.",
            "Minutes 3–4: notice where you feel tension — jaw, shoulders, hands — and let each spot soften a little.",
            "Minute 5: think of one thing you're doing after this, however small.",
        ],
        "minutes": 5,
        "midpoint": "Halfway. You're doing fine — just keep breathing.",
        "completion": "Five minutes, done. The urge may still be there, or it may have softened. Either way, you rode it out.",
        "stop": "That's okay. You gave it some time, and that matters.",
    },
    "cooldown_10": {
        "title": "Ten-minute cooldown",
        "description": "A longer pause, with guidance.",
        "intro": "Ten minutes, at your own pace. Walk if you can; sit if you'd rather. This timer runs in your current session only — it isn't saved.",
        "steps": [
            "Minutes 1–3: walk slowly or sit comfortably. Breathe in for 4, out for 6.",
            "Minutes 4–6: pick one sense and follow it — what do you hear right now? Stay with it.",
            "Minutes 7–9: think of one person you respect, and one small thing you'd like to do today.",
            "Minute 10: slow down. Take three final slow breaths.",
        ],
        "minutes": 10,
        "midpoint": "Halfway there. No rush — there's nowhere else to be right now.",
        "completion": "Ten minutes, done. You chose to wait, and you did. That's the whole exercise.",
        "stop": "Stopping is fine. The time you gave it still counts.",
    },
}

FOLLOWUP_OPTIONS = [
    "I feel better.",
    "About the same.",
    "I feel worse.",
    "I still want access.",
    "I want to talk.",
    "I need immediate human support.",
]

FOLLOWUP_RESPONSES = {
    FOLLOWUP_OPTIONS[0]: ("Good. That's a real win, even if it's a small one.", ["Done", "Talk more"]),
    FOLLOWUP_OPTIONS[1]: ("That's okay. These moments pass — they just don't feel like it while you're in one.", ["Try another exercise", "Talk with companion", "Done"]),
    FOLLOWUP_OPTIONS[2]: ("I'm sorry it's feeling harder right now. You don't have to sit with this alone.", ["Emergency support", "Talk with companion", "Done"]),
    FOLLOWUP_OPTIONS[3]: ("That's an honest answer, and it's okay to feel that way. The block stays on — that's what it's here for. Want to ride it out a little longer?", ["Start a cooldown", "Talk with companion", "Done"]),
}


def route_checkin(selection: str) -> str:
    if selection == ACCIDENTAL_BLOCK:
        return "accidental"
    if selection == LEGITIMATE_ACCESS:
        return "legitimate"
    return "interventions"

