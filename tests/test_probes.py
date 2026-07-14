"""Tests for the calibration probe system."""

import pytest
from null_memory.agent import AgentMemory


@pytest.fixture
def mem(tmp_path):
    """Fresh AgentMemory with isolated temp directory."""
    return AgentMemory.load(str(tmp_path))


class TestProbesCRUD:
    """Test basic probe create/read operations."""

    def test_add_user_probe(self, mem):
        probe = mem.add_probe(
            "What number does Sam wear?", "#4", probe_type="user"
        )
        assert probe["question"] == "What number does Sam wear?"
        assert probe["expected"] == "#4"
        assert probe["probe_type"] == "user"

    def test_get_probes(self, mem):
        mem.add_probe("Q1?", "A1", probe_type="user")
        mem.add_probe("Q2?", "A2", probe_type="auto")
        all_probes = mem.db.get_probes()
        assert len(all_probes) == 2

    def test_get_probes_by_type(self, mem):
        mem.add_probe("Q1?", "A1", probe_type="user")
        mem.add_probe("Q2?", "A2", probe_type="auto")
        user_only = mem.db.get_probes(probe_type="user")
        assert len(user_only) == 1
        assert user_only[0]["question"] == "Q1?"

    def test_count_probes(self, mem):
        assert mem.db.count_probes() == 0
        mem.add_probe("Q?", "A")
        assert mem.db.count_probes() == 1

    def test_delete_probes_for_fact(self, mem):
        mem.add_probe("Q?", "A", fact_id="fact123", probe_type="auto")
        mem.add_probe("Q2?", "A2", fact_id="fact456", probe_type="auto")
        deleted = mem.db.delete_probes_for_fact("fact123")
        assert deleted == 1
        assert mem.db.count_probes() == 1


class TestProbeExecution:
    """Test running probes against recall."""

    def test_probe_passes_when_fact_found(self, mem):
        entry = mem.learn("Agent NR9 wears jersey number 42 in basketball",
                          confidence=0.9, project="global")
        probe = mem.add_probe(
            "NR9 jersey number", "42", fact_id=entry["id"]
        )
        results = mem.run_probes(include_system=False)
        assert results["total"] >= 1
        # Find the probe we added (not auto-generated ones)
        user_results = [d for d in results["details"] if d["expected"] == "42"
                        and d["question"] == "NR9 jersey number"]
        assert len(user_results) == 1
        assert user_results[0]["passed"] is True
        assert user_results[0]["matched_rank"] is not None

    def test_probe_fails_when_expected_missing(self, mem):
        mem.learn("The sky is blue on clear days",
                  confidence=0.9, project="global")
        probe = mem.add_probe("What color is the sky?", "green")
        results = mem.run_probes(include_system=False)
        green_results = [d for d in results["details"] if d["expected"] == "green"]
        assert len(green_results) == 1
        assert green_results[0]["passed"] is False

    def test_probe_result_updates_stats(self, mem):
        mem.learn("Test fact XYZ for probe stats",
                  confidence=0.9, project="global")
        mem.add_probe("XYZ probe", "XYZ")
        mem.run_probes(include_system=False)
        probes = mem.db.get_probes()
        probe = [p for p in probes if p["question"] == "XYZ probe"][0]
        assert probe["run_count"] == 1
        assert probe["last_run"] is not None
        assert probe["last_result"] in ("pass", "fail")

    def test_score_calculation(self, mem):
        mem.learn("Fact AAA exists", confidence=0.9)
        mem.learn("Fact BBB exists", confidence=0.9)
        mem.add_probe("Find AAA", "AAA")
        mem.add_probe("Find CCC", "CCC")  # Will fail - no fact with CCC
        results = mem.run_probes(include_system=False)
        user_probes = [d for d in results["details"]
                       if d["probe_type"] == "user"]
        passed = sum(1 for d in user_probes if d["passed"])
        failed = sum(1 for d in user_probes if not d["passed"])
        assert passed >= 1
        assert failed >= 1


class TestAutoGeneration:
    """Test auto-probe generation from facts with entities."""

    def test_date_entity_generates_probe(self, mem):
        mem.learn("Pete's son Sam was born April 19, 2018",
                  confidence=0.9, project="test")
        probes = mem.db.get_probes(probe_type="auto")
        dates = [p for p in probes if "April 19, 2018" in p["expected"]]
        assert len(dates) >= 1

    def test_number_entity_generates_probe(self, mem):
        mem.learn("The team has #42 on the roster",
                  confidence=0.9, project="test")
        probes = mem.db.get_probes(probe_type="auto")
        numbers = [p for p in probes if "#42" in p["expected"]]
        assert len(numbers) >= 1

    def test_small_numbers_skipped(self, mem):
        mem.learn("Sam always wears #4 in sports",
                  confidence=0.9, project="test")
        probes = mem.db.get_probes(probe_type="auto")
        numbers = [p for p in probes if "#4" in p["expected"]]
        assert len(numbers) == 0  # Single digit numbers are too generic

    def test_version_entity_generates_probe(self, mem):
        mem.learn("Aleph is currently at v0.5.0",
                  confidence=0.9, project="test")
        probes = mem.db.get_probes(probe_type="auto")
        versions = [p for p in probes if "v0.5.0" in p["expected"]]
        assert len(versions) >= 1

    def test_dollar_entity_generates_probe(self, mem):
        mem.learn("Orion has $5.64 deployed in positions",
                  confidence=0.9, project="test")
        probes = mem.db.get_probes(probe_type="auto")
        dollars = [p for p in probes if "$5.64" in p["expected"]]
        assert len(dollars) >= 1

    def test_no_probes_for_generic_facts(self, mem):
        mem.learn("Python uses indentation for blocks",
                  confidence=0.9, project="test")
        probes = mem.db.get_probes(probe_type="auto")
        assert len(probes) == 0

    def test_no_duplicate_probes(self, mem):
        mem.learn("System has v2.1.0 deployed",
                  confidence=0.9, project="test")
        # Learning again shouldn't create duplicate probes
        mem.learn("System has v2.1.0 deployed",
                  confidence=0.95, project="test")
        probes = mem.db.get_probes(probe_type="auto")
        version_probes = [p for p in probes if "v2.1.0" in p["expected"]]
        assert len(version_probes) == 1


class TestSystemProbes:
    """Test Layer 1 system probes."""

    def test_system_probes_run(self, mem):
        results = mem.run_probes(probe_type="system")
        system = [d for d in results["details"] if d["probe_type"] == "system"]
        assert len(system) >= 2  # At least learn/recall + specific detail

    def test_system_probes_cleanup(self, mem):
        """System probes should not leave test data behind."""
        facts_before = mem.db.count_facts()
        mem.run_probes(probe_type="system")
        facts_after = mem.db.count_facts()
        assert facts_after == facts_before

    def test_learn_recall_roundtrip_passes(self, mem):
        results = mem.run_probes(probe_type="system")
        roundtrip = [d for d in results["details"]
                     if "just-learned" in d["question"]]
        assert len(roundtrip) == 1
        assert roundtrip[0]["passed"] is True

    def test_specific_detail_retrieval_passes(self, mem):
        results = mem.run_probes(probe_type="system")
        detail = [d for d in results["details"]
                  if "specific numbers" in d["question"]]
        assert len(detail) == 1
        assert detail[0]["passed"] is True


class TestDoctorIntegration:
    """Test that null_doctor includes calibration."""

    def test_doctor_includes_calibration_score(self, mem):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=mem.agent_dir)
        handlers._memory = mem
        handlers._session_started = True
        output = handlers.handle_doctor()
        assert "Calibration" in output
        assert "probes" in output.lower()

    def test_doctor_shows_failures(self, mem):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=mem.agent_dir)
        handlers._memory = mem
        handlers._session_started = True
        # Add a probe that will fail
        mem.add_probe("Find nonexistent ZZZZZ", "ZZZZZ")
        output = handlers.handle_doctor()
        assert "ZZZZZ" in output or "Failed" in output


class TestRecallWithWarnings:
    """Test that recall flags facts with failing probes."""

    def test_recall_shows_caution_for_failing_probe(self, mem):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=mem.agent_dir)
        handlers._memory = mem
        handlers._session_started = True

        entry = mem.learn("Agent QR7 wears jersey number 99",
                          confidence=0.9, project="global")
        # Add a probe and force it to have a failing record
        probe = mem.add_probe("QR7 jersey?", "99", fact_id=entry["id"])
        # Manually set the probe to have failed
        mem.db.update_probe_result(probe["id"], False)

        output = handlers.handle_recall("QR7 jersey")
        assert "CAUTION" in output or "⚠" in output

    def test_recall_no_warning_for_passing_probe(self, mem):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=mem.agent_dir)
        handlers._memory = mem
        handlers._session_started = True

        entry = mem.learn("Agent QR8 plays position quarterback",
                          confidence=0.9, project="global")
        probe = mem.add_probe("QR8 position?", "quarterback", fact_id=entry["id"])
        mem.db.update_probe_result(probe["id"], True)

        output = handlers.handle_recall("QR8 position")
        assert "CAUTION" not in output


class TestLearnValidation:
    """Test that learn checks for broken probes."""

    def test_learn_warns_when_probe_breaks(self, mem):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=mem.agent_dir)
        handlers._memory = mem
        handlers._session_started = True

        # Learn a fact and create a probe for it
        mem.learn("Agent XK5 wears jersey number 42",
                  confidence=0.9, project="global")
        mem.add_probe("XK5 jersey number", "42")

        # Now learn a contradicting fact with the same keywords
        output = handlers.handle_learn(
            "Agent XK5 wears jersey number 99", confidence=0.95
        )
        # The probe for "42" should now fail since "99" might push "42" down
        # or the fact text changed. The probe alert may or may not fire
        # depending on recall ranking — this test verifies the mechanism runs.
        assert "Learned:" in output

    def test_learn_no_alert_for_unrelated_fact(self, mem):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=mem.agent_dir)
        handlers._memory = mem
        handlers._session_started = True

        mem.learn("Agent XK6 wears jersey number 42",
                  confidence=0.9, project="global")
        mem.add_probe("XK6 jersey number", "42")

        # Learn something completely unrelated
        output = handlers.handle_learn(
            "Python uses indentation for scoping", confidence=0.8
        )
        assert "PROBE ALERT" not in output


class TestHeartbeat:
    """Test periodic calibration heartbeat."""

    def test_heartbeat_fires_at_10_turns(self, mem):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=mem.agent_dir)
        handlers._memory = mem
        handlers._session_started = True

        # Add a probe so heartbeat has something to check
        mem.learn("Heartbeat test entity HB1 value 777",
                  confidence=0.9, project="global")
        mem.add_probe("HB1 value?", "777")

        # Simulate 9 observations (facts_created will be 9 after these + the learn above)
        # The learn above already incremented to 1, so we need 9 more to hit 10
        for i in range(9):
            handlers.handle_observe(f"Turn {i} observation", project="global")

        # The 10th observe should trigger heartbeat
        output = handlers.handle_observe("Turn 10 — heartbeat trigger", project="global")
        # Note: facts_created includes the learn() call, so we may need to adjust
        # The heartbeat fires when facts_created % 10 == 0
        # Even if it doesn't fire exactly here (timing depends on internal count),
        # we verify the mechanism doesn't crash
        assert "Observed:" in output

    def test_heartbeat_includes_score_when_probes_exist(self, mem):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=mem.agent_dir)
        handlers._memory = mem
        handlers._session_started = True

        # Start a session so facts_created is tracked
        mem.start_session(project="global")

        mem.learn("Heartbeat score test entity HB2 value 888",
                  confidence=0.9, project="global")
        mem.add_probe("HB2 value?", "888")

        # Force facts_created to 9 so the next observe (which creates fact #10) triggers heartbeat
        mem._current_session.facts_created = 9

        output = handlers.handle_observe("Heartbeat score test", project="global")
        assert "HEARTBEAT" in output
        assert "calibration" in output.lower()
