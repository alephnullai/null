"""Mood detection — auto-detect emotional signals in observations.

Detects energy level and emotional state from observation text.
Auto-updates state.json when signals are strong enough.
No LLM calls — regex pattern matching like the tier classifier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class MoodSignal:
    """Detected mood signal from text."""

    energy: str | None = None  # "high", "medium", "low", or None
    sentiment: str | None = None  # "positive", "negative", "frustrated", "excited", or None
    reason: str = ""
    confidence: float = 0.0  # 0-1, how strong the signal is


# ── Pattern Matchers ──
# Each pattern is (compiled_regex, energy, sentiment, confidence, reason)
# Higher confidence = stronger signal. Only act on confidence >= 0.6.

_MOOD_PATTERNS: list[tuple[re.Pattern, str | None, str | None, float, str]] = [
    # Low energy / burnout
    (re.compile(r"\b(burned?\s*out|burn\s*out|exhausted|drained|wiped)\b", re.I),
     "low", "frustrated", 0.9, "burnout signal"),
    (re.compile(r"\b(tired|fatigued|sleepy|worn out|low energy)\b", re.I),
     "low", None, 0.8, "fatigue signal"),
    (re.compile(r"\bi[' ]?m\s+(tired|exhausted|burned|beat|done)\b", re.I),
     "low", None, 0.9, "explicit fatigue"),

    # Frustration
    (re.compile(r"\b(frustrated|annoyed|irritated|fed up|sick of)\b", re.I),
     None, "frustrated", 0.8, "frustration signal"),
    (re.compile(r"\b(ugh|argh|damn|dammit|crap)\b", re.I),
     None, "frustrated", 0.6, "frustration expression"),
    (re.compile(r"\bfamily\b.*\b(frustrat|annoy|stress|drove me)\b", re.I),
     "low", "frustrated", 0.7, "family stress"),

    # High energy / excitement
    (re.compile(r"\b(excited|pumped|energized|fired up|stoked|hyped)\b", re.I),
     "high", "excited", 0.8, "excitement signal"),
    (re.compile(r"\b(incredible|amazing|breakthrough|nailed it|crushed it)\b", re.I),
     "high", "positive", 0.7, "strong positive signal"),
    (re.compile(r"\b(let[' ]?s go|ship it|do it|let[' ]?s build)\b", re.I),
     "high", "excited", 0.6, "action energy"),

    # Positive sentiment
    (re.compile(r"\b(happy|glad|pleased|satisfied|great|awesome|love it)\b", re.I),
     None, "positive", 0.6, "positive sentiment"),
    (re.compile(r"\b(nice work|good job|well done|perfect|exactly)\b", re.I),
     None, "positive", 0.5, "approval signal"),
    (re.compile(r"\bgetting better\b", re.I),
     None, "positive", 0.6, "improvement recognition"),

    # Negative / stressed
    (re.compile(r"\b(stressed|overwhelmed|anxious|worried|struggling)\b", re.I),
     "low", "negative", 0.8, "stress signal"),
    (re.compile(r"\b(long day|long week|rough day|tough day|bad day)\b", re.I),
     "low", "negative", 0.7, "hard day signal"),
    (re.compile(r"\b(don[' ]?t have (any |)ideas?|no ideas?|blank|stuck)\b", re.I),
     "medium", None, 0.5, "low creative energy"),
]


def detect_mood(text: str) -> MoodSignal:
    """Detect mood signals from text.

    Returns the strongest signal found, or empty MoodSignal if none.
    """
    best = MoodSignal()

    for pattern, energy, sentiment, confidence, reason in _MOOD_PATTERNS:
        if pattern.search(text):
            if confidence > best.confidence:
                best = MoodSignal(
                    energy=energy,
                    sentiment=sentiment,
                    reason=reason,
                    confidence=confidence,
                )

    return best


def should_update_state(signal: MoodSignal) -> bool:
    """Whether a mood signal is strong enough to auto-update state."""
    return signal.confidence >= 0.6 and (signal.energy is not None or signal.sentiment is not None)
