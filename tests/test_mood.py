"""Tests for mood detection."""

import pytest
from null_memory.mood import detect_mood, should_update_state, MoodSignal


class TestDetectMood:
    def test_burnout(self):
        signal = detect_mood("Pete is burned out from a long day with family")
        assert signal.energy == "low"
        assert signal.confidence >= 0.7

    def test_tired(self):
        signal = detect_mood("I'm tired, been running around all day")
        assert signal.energy == "low"
        assert signal.confidence >= 0.8

    def test_frustrated(self):
        signal = detect_mood("my family has frustrated me today")
        assert signal.sentiment == "frustrated"
        assert signal.confidence >= 0.6

    def test_excited(self):
        signal = detect_mood("I'm excited about this new feature idea")
        assert signal.energy == "high"
        assert signal.sentiment == "excited"

    def test_positive(self):
        signal = detect_mood("You seem to be getting better and better")
        assert signal.sentiment == "positive"

    def test_stressed(self):
        signal = detect_mood("feeling really stressed and overwhelmed")
        assert signal.energy == "low"
        assert signal.sentiment == "negative"

    def test_no_signal(self):
        signal = detect_mood("let's implement the database migration")
        assert signal.confidence < 0.6

    def test_empty_text(self):
        signal = detect_mood("")
        assert signal.confidence == 0.0

    def test_action_energy(self):
        signal = detect_mood("let's go build this thing")
        assert signal.energy == "high"

    def test_long_day(self):
        signal = detect_mood("it's been a long day of running errands")
        assert signal.energy == "low"


class TestShouldUpdateState:
    def test_strong_signal_updates(self):
        signal = MoodSignal(energy="low", sentiment="frustrated",
                            confidence=0.8, reason="test")
        assert should_update_state(signal) is True

    def test_weak_signal_no_update(self):
        signal = MoodSignal(energy=None, sentiment=None,
                            confidence=0.3, reason="")
        assert should_update_state(signal) is False

    def test_no_actionable_fields(self):
        signal = MoodSignal(energy=None, sentiment=None,
                            confidence=0.9, reason="test")
        assert should_update_state(signal) is False
