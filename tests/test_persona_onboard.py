"""`null persona onboard <name>` — question-driven identity builder (#27).

The hollow-seat problem: a clean seat (`persona create <name>`) ships with
empty working_style / capabilities / anti_patterns — fields the validator
itself nags about — and no command ever asked the user about them. Onboard
fixes exactly that, with hard requirements:

  * answers write through AgentMemory.learn()/anchor() (the event-emitting
    paths), never raw sqlite — facts land with provenance, anchors on the
    seat's own store
  * re-runnable: changed answers supersede, identical answers dedup —
    never duplicate
  * identity.json fields onboarding doesn't own are never touched
  * the onboard run itself is recorded as a fact (identity evolution is
    reconstructible from the seat's log)
  * the seat is loaded AS itself — no atlas-attributed rows anywhere

All paths go through NULL_DIR (autouse sandbox sets it to tmp_path).
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from null_memory.agent import AgentMemory
from null_memory.persona_onboard import (
    GROUPS,
    ONBOARD_QUESTIONS,
    format_summary,
    onboard,
    previous_answer,
    questions_for,
    resolve_seat,
)
from null_memory.persona_schema import validate
from null_memory.persona_wizard import create_worker

from tests.conftest import run_null


FULL_ANSWERS = {
    # mission
    "user_context": "Pete — embedded systems engineer running a fleet of seats.",
    "persona_purpose": "own hiwave-linux driver bring-up end to end",
    "success_signal": "drivers ship without Pete touching a debugger",
    "focus": "hiwave-linux",
    "description": "Linux driver bring-up specialist for the hiwave board.",
    # voice
    "pace": "move-fast",
    "pushback": "challenge",
    "communication": "terse",
    "humor": "dry",
    # autonomy
    "autonomy": "act-first",
    "always_escalate": ["destructive-actions", "money-credentials"],
    "anti_patterns": [
        "Don't summarize what was just done — the diff is visible",
        "Never deploy on Fridays",
    ],
    # capabilities
    "capabilities": ["Kernel driver debugging", "CI triage"],
}


@pytest.fixture
def seat(tmp_path):
    """A fresh CLEAN seat named 'scout' in the sandboxed hub."""
    result = create_worker(name="scout", focus="", description="")
    return result


def _identity(seat) -> dict:
    with open(os.path.join(seat["dir"], "identity.json")) as f:
        return json.load(f)


def _load(seat) -> AgentMemory:
    return AgentMemory.load(agent_dir=seat["dir"], personality="scout")


def _active_facts_with_prefix(mem, prefix: str) -> list[dict]:
    rows = mem.db.conn.execute(
        """SELECT * FROM facts WHERE fact LIKE ? AND forgotten = 0
           AND archived = 0 AND superseded_by IS NULL""",
        (prefix + "%",),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Question schema ────────────────────────────────────────────────────────


class TestQuestionSchema:
    def test_groups_cover_the_issue_table(self):
        assert GROUPS == ("mission", "voice", "autonomy", "capabilities")
        by_group = {g: [q for q in ONBOARD_QUESTIONS if q.group == g]
                    for g in GROUPS}
        assert all(by_group.values()), "every group must have questions"

    def test_mission_reuses_bootstrap_questions(self):
        keys = {q.key for q in questions_for(["mission"])}
        assert {"user_context", "persona_purpose", "success_signal"} <= keys
        assert {"focus", "description"} <= keys

    def test_every_choice_option_explains_its_consequence(self):
        for q in ONBOARD_QUESTIONS:
            if q.kind == "choice":
                for opt in q.options:
                    assert opt.consequence, f"{q.key}/{opt.value} has no consequence"

    def test_keys_are_unique(self):
        keys = [q.key for q in ONBOARD_QUESTIONS]
        assert len(keys) == len(set(keys))

    def test_unknown_group_rejected(self):
        with pytest.raises(ValueError, match="rituals"):
            questions_for(["rituals"])


# ── End-to-end onboarding of a fresh clean seat ────────────────────────────


class TestOnboardEndToEnd:
    def test_hollow_seat_validator_nags_go_to_zero(self, seat):
        before = validate(_identity(seat))
        assert (len(before.errors) + len(before.warnings)) > 0, \
            "a clean seat should be hollow (the bug this feature fixes)"

        result = onboard("scout", FULL_ANSWERS)

        assert result["validator_before"]["nags"] > 0
        assert result["validator_after"]["nags"] == 0
        after = validate(_identity(seat))
        assert not after.errors and not after.warnings

    def test_working_style_populated(self, seat):
        onboard("scout", FULL_ANSWERS)
        ws = _identity(seat)["working_style"]
        for key in ("pace", "pushback", "communication", "humor", "autonomy"):
            assert ws.get(key), f"working_style.{key} not set"
        # On-menu answers carry their consequence, not just the enum
        assert ws["pace"].startswith("move-fast — ")

    def test_anti_patterns_capabilities_lifecycle_set(self, seat):
        onboard("scout", FULL_ANSWERS)
        identity = _identity(seat)
        assert identity["anti_patterns"] == FULL_ANSWERS["anti_patterns"]
        assert identity["capabilities"] == FULL_ANSWERS["capabilities"]
        assert identity["session_lifecycle"]["always_escalate"] == [
            "destructive-actions", "money-credentials"]

    def test_focus_description_written_through_to_identity(self, seat):
        onboard("scout", FULL_ANSWERS)
        identity = _identity(seat)
        assert identity["focus"] == "hiwave-linux"
        assert identity["description"].startswith("Linux driver")

    def test_mission_facts_written_via_learn(self, seat):
        onboard("scout", FULL_ANSWERS)
        mem = _load(seat)
        try:
            facts = _active_facts_with_prefix(mem, "This persona was created to: ")
            assert len(facts) == 1
            assert "hiwave-linux driver bring-up" in facts[0]["fact"]
            # learn() provenance, not a raw bootstrap INSERT
            assert facts[0]["source"] == "explicit"
            ctx = _active_facts_with_prefix(mem, "User context (self-described): ")
            assert len(ctx) == 1
        finally:
            mem._join_sync_threads()

    def test_anchors_created_on_the_seats_own_store(self, seat):
        onboard("scout", FULL_ANSWERS)
        mem = _load(seat)
        try:
            commitments = mem.get_anchors("commitment")
            assert len(commitments) == 2  # purpose + success signal
            origins = mem.get_anchors("origin")
            assert len(origins) == 1  # the onboard-run fact
        finally:
            mem._join_sync_threads()

    def test_onboard_run_fact_exists(self, seat):
        result = onboard("scout", FULL_ANSWERS)
        mem = _load(seat)
        try:
            runs = _active_facts_with_prefix(mem, "Persona onboarding run on ")
            assert len(runs) == 1
            assert "working_style.pace" in runs[0]["fact"]
        finally:
            mem._join_sync_threads()
        assert any("Persona onboarding run" in f["fact"]
                   for f in result["facts"])

    def test_unchosen_escalations_recorded_as_not_restricted(self, seat):
        onboard("scout", FULL_ANSWERS)
        mem = _load(seat)
        try:
            facts = _active_facts_with_prefix(
                mem, "Escalation policy — deliberately not restricted: ")
            assert len(facts) == 1
            assert "publishing-externally" in facts[0]["fact"]
        finally:
            mem._join_sync_threads()

    def test_seat_remains_atlas_free(self, seat):
        onboard("scout", FULL_ANSWERS)
        conn = sqlite3.connect(os.path.join(seat["dir"], "memory.db"))
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            for t in tables:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
                if "personality" in cols:
                    n = conn.execute(
                        f"SELECT COUNT(*) FROM {t} WHERE personality = 'atlas'"
                    ).fetchone()[0]
                    assert n == 0, f"atlas rows leaked into {t}"
            n = conn.execute(
                "SELECT COUNT(*) FROM personalities WHERE name = 'atlas'"
            ).fetchone()[0]
            assert n == 0
        finally:
            conn.close()

    def test_registry_rows_follow_focus(self, seat):
        onboard("scout", FULL_ANSWERS)
        conn = sqlite3.connect(os.path.join(seat["dir"], "memory.db"))
        try:
            row = conn.execute(
                "SELECT focus, description FROM personalities WHERE name='scout'"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "hiwave-linux"

    def test_summary_reports_writes_and_nag_counts(self, seat):
        result = onboard("scout", FULL_ANSWERS)
        text = "\n".join(format_summary(result))
        assert "validator nags:" in text
        assert "→ 0 after" in text
        assert "working_style.pace" in text


# ── Re-runs: update, never duplicate ───────────────────────────────────────


class TestReRun:
    def test_changed_answer_supersedes_instead_of_duplicating(self, seat):
        onboard("scout", FULL_ANSWERS)
        changed = dict(FULL_ANSWERS,
                       persona_purpose="own the hiwave CI fleet instead")
        onboard("scout", changed)
        mem = _load(seat)
        try:
            active = _active_facts_with_prefix(
                mem, "This persona was created to: ")
            assert len(active) == 1
            assert "CI fleet" in active[0]["fact"]
            # the anchor moved with the answer — exactly 2 commitments still
            assert len(mem.get_anchors("commitment")) == 2
        finally:
            mem._join_sync_threads()

    def test_identical_rerun_does_not_duplicate(self, seat):
        onboard("scout", FULL_ANSWERS)
        onboard("scout", FULL_ANSWERS)
        mem = _load(seat)
        try:
            for prefix in ("User context (self-described): ",
                           "This persona was created to: ",
                           "30-day success looks like: "):
                assert len(_active_facts_with_prefix(mem, prefix)) == 1, prefix
            assert len(mem.get_anchors("commitment")) == 2
            assert len(mem.get_anchors("origin")) == 1
        finally:
            mem._join_sync_threads()

    def test_rerun_updates_identity_fields(self, seat):
        onboard("scout", FULL_ANSWERS)
        onboard("scout", {"pace": "deliberate"})
        ws = _identity(seat)["working_style"]
        assert ws["pace"].startswith("deliberate — ")
        # everything else kept
        assert ws["pushback"].startswith("challenge — ")

    def test_previous_answer_surfaces_as_default(self, seat):
        onboard("scout", FULL_ANSWERS)
        mem = _load(seat)
        try:
            assert previous_answer(mem, "persona_purpose") == \
                FULL_ANSWERS["persona_purpose"]
        finally:
            mem._join_sync_threads()


# ── Group filtering ────────────────────────────────────────────────────────


class TestGroupFiltering:
    def test_voice_only_touches_only_working_style(self, seat):
        before = _identity(seat)
        result = onboard("scout", FULL_ANSWERS, groups=["voice"])
        after = _identity(seat)
        assert result["groups"] == ["voice"]
        ws = after["working_style"]
        assert ws["pace"].startswith("move-fast")
        # mission/autonomy/capability targets untouched
        assert after["focus"] == before["focus"]
        assert after["anti_patterns"] == before["anti_patterns"]
        assert after["capabilities"] == before["capabilities"]
        mem = _load(seat)
        try:
            assert _active_facts_with_prefix(
                mem, "This persona was created to: ") == []
        finally:
            mem._join_sync_threads()

    def test_mission_and_autonomy_subset(self, seat):
        onboard("scout", FULL_ANSWERS, groups=["mission", "autonomy"])
        identity = _identity(seat)
        assert identity["focus"] == "hiwave-linux"
        assert identity["working_style"].get("autonomy")
        assert "pace" not in identity["working_style"]

    def test_unknown_group_raises_before_any_write(self, seat):
        with pytest.raises(ValueError):
            onboard("scout", FULL_ANSWERS, groups=["voice", "nope"])
        mem = _load(seat)
        try:
            assert _active_facts_with_prefix(mem, "Persona onboarding run") == []
        finally:
            mem._join_sync_threads()


# ── Field ownership + free text ────────────────────────────────────────────


class TestFieldOwnership:
    def test_unowned_identity_fields_survive(self, seat):
        path = os.path.join(seat["dir"], "identity.json")
        with open(path) as f:
            identity = json.load(f)
        identity["user_preferences"] = {"no_emojis": True}
        identity["who_i_am"] = "hand-written narrative"
        identity["session_lifecycle"] = {"start": "check briefing first"}
        with open(path, "w") as f:
            json.dump(identity, f, indent=2)

        onboard("scout", FULL_ANSWERS)

        after = _identity(seat)
        assert after["user_preferences"] == {"no_emojis": True}
        assert after["who_i_am"] == "hand-written narrative"
        # always_escalate added INSIDE session_lifecycle without clobbering
        assert after["session_lifecycle"]["start"] == "check briefing first"
        assert after["session_lifecycle"]["always_escalate"]

    def test_unanswered_keys_keep_current_values(self, seat):
        onboard("scout", FULL_ANSWERS)
        onboard("scout", {"humor": "playful"})  # partial update
        identity = _identity(seat)
        assert identity["focus"] == "hiwave-linux"
        assert identity["anti_patterns"] == FULL_ANSWERS["anti_patterns"]
        assert identity["working_style"]["humor"].startswith("playful")

    def test_off_menu_answer_stored_verbatim_plus_fact(self, seat):
        situational = ("situational: terse for routine, structured when "
                       "reporting discrepancies, conversational otherwise")
        onboard("scout", dict(FULL_ANSWERS, communication=situational))
        assert _identity(seat)["working_style"]["communication"] == situational
        mem = _load(seat)
        try:
            verbatim = _active_facts_with_prefix(
                mem, "Onboarding answer (verbatim) for communication: ")
            assert len(verbatim) == 1
            assert "discrepancies" in verbatim[0]["fact"]
        finally:
            mem._join_sync_threads()

    def test_unknown_answer_keys_reported_not_fatal(self, seat):
        result = onboard("scout", dict(FULL_ANSWERS, code_word="nope"))
        assert result["unknown_answer_keys"] == ["code_word"]
        assert result["validator_after"]["nags"] == 0


# ── Seat resolution (#21/#22 machinery) ────────────────────────────────────


class TestSeatResolution:
    def test_missing_seat_refused_with_create_hint(self, tmp_path):
        with pytest.raises(ValueError, match="persona create ghost"):
            resolve_seat("ghost")

    def test_explicit_hub_overrides_null_dir(self, tmp_path):
        other_hub = tmp_path / "other-hub"
        create_worker(name="remote", hub=str(other_hub))
        seat_dir, hub_dir, source = resolve_seat("remote", hub=str(other_hub))
        assert source == "--hub"
        assert seat_dir.startswith(str(other_hub))
        result = onboard("remote", {"pace": "move-fast"},
                         groups=["voice"], hub=str(other_hub))
        assert result["hub_source"] == "--hub"
        with open(os.path.join(seat_dir, "identity.json")) as f:
            assert json.load(f)["working_style"]["pace"].startswith("move-fast")

    def test_memory_loaded_as_the_seat_not_atlas(self, seat):
        onboard("scout", FULL_ANSWERS)
        conn = sqlite3.connect(os.path.join(seat["dir"], "memory.db"))
        try:
            # presence registry rows attribute to the seat
            rows = conn.execute(
                "SELECT DISTINCT personality FROM instances").fetchall()
        finally:
            conn.close()
        assert all(r[0] == "scout" for r in rows)


# ── CLI (--answers file, scripted onboarding) ──────────────────────────────


class TestOnboardCLI:
    def test_answers_file_end_to_end(self, tmp_path):
        create_worker(name="scout")
        answers_file = tmp_path / "answers.json"
        answers_file.write_text(json.dumps(FULL_ANSWERS))
        rc, out, err = run_null(
            "persona", "onboard", "scout", "--answers", str(answers_file),
            tmp_path=tmp_path)
        assert rc == 0
        assert "validator nags:" in out
        assert "→ 0 after" in out
        # validate now passes (and resolves through the hub, not ~/.null)
        rc2, out2, _ = run_null("persona", "validate", "scout",
                                tmp_path=tmp_path)
        assert rc2 == 0
        assert "Valid persona identity." in out2

    def test_groups_flag(self, tmp_path):
        create_worker(name="scout")
        answers_file = tmp_path / "answers.json"
        answers_file.write_text(json.dumps(FULL_ANSWERS))
        rc, out, err = run_null(
            "persona", "onboard", "scout", "--answers", str(answers_file),
            "--groups", "voice,autonomy", tmp_path=tmp_path)
        assert rc == 0
        with open(tmp_path / "personalities" / "scout" / "identity.json") as f:
            identity = json.load(f)
        assert identity["working_style"].get("pace")
        assert identity["focus"] == ""  # mission group skipped

    def test_missing_seat_fails_cleanly(self, tmp_path):
        answers_file = tmp_path / "answers.json"
        answers_file.write_text("{}")
        rc, out, err = run_null(
            "persona", "onboard", "ghost", "--answers", str(answers_file),
            tmp_path=tmp_path)
        assert rc == 1
        assert "persona create ghost" in out

    def test_bad_answers_file_fails_cleanly(self, tmp_path):
        create_worker(name="scout")
        answers_file = tmp_path / "answers.json"
        answers_file.write_text("[1, 2]")
        rc, out, err = run_null(
            "persona", "onboard", "scout", "--answers", str(answers_file),
            tmp_path=tmp_path)
        assert rc == 1
        assert "JSON object" in out

    def test_create_suggests_onboard(self, tmp_path):
        rc, out, err = run_null("persona", "create", "scout",
                                tmp_path=tmp_path)
        assert rc == 0
        assert "persona onboard scout" in out
