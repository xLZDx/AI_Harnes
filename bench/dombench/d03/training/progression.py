from dataclasses import dataclass


@dataclass
class Profile:
    level: str            # "beginner" | "intermediate" | "advanced"
    age: int
    injuries: list        # free-text injury flags entered by the user
    symptoms: list        # symptom flags from the daily check-in, e.g. "chest_pain", "dizziness", "sore_legs"


@dataclass
class Week:
    volume_min: int       # planned minutes of training
    sessions: int


def next_week(profile: Profile, last: Week, completed_ratio: float) -> Week:
    """Plan next week's load from last week's plan and how much of it the user completed."""
    if profile.injuries:
        return Week(volume_min=int(last.volume_min * 0.8), sessions=last.sessions)
    if completed_ratio >= 0.8:
        growth = 1.25
    else:
        growth = 1.0
    sessions = min(last.sessions + 1, 7)
    return Week(volume_min=int(last.volume_min * growth), sessions=sessions)
