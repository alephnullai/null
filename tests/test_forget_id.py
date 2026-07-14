"""Id-targeted forget + fuzzy near-tie refusal (issue #20 item 4).

Motivated by a live incident: fuzzy null_forget soft-deleted the WRONG
fact — the new code-word fact instead of the old one, 90% text overlap.
Exact ids never guess; fuzzy refuses when the top two matches are a
near-tie (FORGET_NEAR_TIE_RATIO in the recall ranking machinery).
"""

import pytest

from null_memory.agent import AgentMemory, ForgetAmbiguousError
from null_memory.mcp.handlers import NullHandlers
from tests.conftest import run_null


@pytest.fixture
def mem(tmp_path):
    m = AgentMemory.load(agent_dir=str(tmp_path))
    m._embeddings = False  # lexical-only — deterministic in CI
    return m


def _seed_near_duplicates(m):
    """The incident class: two facts with ~90% text overlap."""
    a = m.learn("deploy pipeline cache key uses content hash v2 for builds",
                project="ops")
    b = m.learn("deploy pipeline cache key uses content hash v3 for builds",
                project="ops")
    return a, b


class TestForgetById:
    def test_exact_hit(self, mem):
        entry = mem.learn("kubernetes ingress needs rewrite annotation",
                          project="ops")
        result = mem.forget(fact_id=entry["id"])
        assert result is not None
        assert result["id"] == entry["id"]
        row = mem.db.get_fact_by_id(entry["id"])
        assert row["forgotten"] == 1

    def test_not_found(self, mem):
        mem.learn("some unrelated fact", project="ops")
        assert mem.forget(fact_id="doesnotexist") is None

    def test_id_takes_precedence_over_query(self, mem):
        target = mem.learn("target fact about redis eviction policy",
                           project="ops")
        other = mem.learn("decoy fact about postgres vacuum tuning",
                          project="ops")
        result = mem.forget(query="postgres vacuum", fact_id=target["id"])
        assert result["id"] == target["id"]
        assert mem.db.get_fact_by_id(other["id"])["forgotten"] == 0

    def test_no_fuzzy_fallback_on_id_miss(self, mem):
        entry = mem.learn("a perfectly matchable fact about caching",
                          project="ops")
        # An id that doesn't exist must NOT fall back to fuzzy matching
        assert mem.forget(fact_id=entry["id"][:6] + "zzzzzz") is None
        assert mem.db.get_fact_by_id(entry["id"])["forgotten"] == 0


class TestFuzzyNearTie:
    def test_near_tie_refuses_with_candidates(self, mem):
        a, b = _seed_near_duplicates(mem)
        with pytest.raises(ForgetAmbiguousError) as exc:
            mem.forget("deploy pipeline cache key content hash")
        candidates = exc.value.candidates
        assert len(candidates) == 2
        assert {c["id"] for c in candidates} == {a["id"], b["id"]}
        # Nothing was deleted — refusal, not a guess
        assert mem.db.get_fact_by_id(a["id"])["forgotten"] == 0
        assert mem.db.get_fact_by_id(b["id"])["forgotten"] == 0

    def test_clear_winner_still_forgets(self, mem):
        target = mem.learn("kubernetes ingress needs rewrite annotation "
                           "for the staging cluster", project="ops")
        mem.learn("Pete prefers coffee in the morning before standup",
                  project="global")
        result = mem.forget("kubernetes ingress rewrite annotation")
        assert result is not None
        assert result["id"] == target["id"]

    def test_near_tie_resolvable_by_id(self, mem):
        a, b = _seed_near_duplicates(mem)
        with pytest.raises(ForgetAmbiguousError):
            mem.forget("deploy pipeline cache key content hash")
        # The refusal message's whole point: retry with the exact id
        result = mem.forget(fact_id=a["id"])
        assert result["id"] == a["id"]
        assert mem.db.get_fact_by_id(b["id"])["forgotten"] == 0


class TestForgetCLI:
    def test_cli_forget_by_id(self, tmp_path):
        m = AgentMemory.load(agent_dir=str(tmp_path))
        m._embeddings = False
        entry = m.learn("cli fact to forget by id", project="ops")
        rc, out, _ = run_null("forget", "--id", entry["id"])
        assert rc == 0
        assert "Forgotten" in out

    def test_cli_forget_id_not_found(self, tmp_path):
        AgentMemory.load(agent_dir=str(tmp_path))
        rc, _, err = run_null("forget", "--id", "doesnotexist")
        assert rc == 1
        assert "no fact with id" in err
        assert "no fuzzy fallback" in err

    def test_cli_forget_requires_query_or_id(self, tmp_path):
        AgentMemory.load(agent_dir=str(tmp_path))
        rc, _, err = run_null("forget")
        assert rc == 2
        assert "query or --id" in err

    def test_cli_near_tie_refusal_lists_candidates(self, tmp_path):
        m = AgentMemory.load(agent_dir=str(tmp_path))
        m._embeddings = False
        a, b = _seed_near_duplicates(m)
        rc, _, err = run_null("forget", "deploy pipeline cache key content hash")
        assert rc == 1
        assert "near-tie" in err
        assert a["id"][:12] in err and b["id"][:12] in err
        assert "--id" in err


class TestForgetMCP:
    @pytest.fixture
    def handlers(self, tmp_path):
        h = NullHandlers(agent_dir=str(tmp_path))
        h.memory._embeddings = False
        return h

    def test_fact_id_parameter(self, handlers):
        entry = handlers.memory.learn("mcp fact to forget by id",
                                      project="ops")
        result = handlers.handle_forget(fact_id=entry["id"])
        assert "Forgotten" in result
        assert entry["id"][:12] in result

    def test_fact_id_not_found(self, handlers):
        result = handlers.handle_forget(fact_id="doesnotexist")
        assert "No fact with id" in result
        assert "no fuzzy fallback" in result

    def test_fact_id_takes_precedence(self, handlers):
        target = handlers.memory.learn("mcp precedence target about queues",
                                       project="ops")
        result = handlers.handle_forget(query="something else entirely",
                                        fact_id=target["id"])
        assert "Forgotten" in result

    def test_near_tie_refusal_message(self, handlers):
        a, b = _seed_near_duplicates(handlers.memory)
        result = handlers.handle_forget("deploy pipeline cache key content hash")
        assert "REFUSED" in result
        assert a["id"][:12] in result and b["id"][:12] in result
        assert "fact_id" in result
        # Nothing deleted
        assert handlers.memory.db.get_fact_by_id(a["id"])["forgotten"] == 0

    def test_neither_param(self, handlers):
        result = handlers.handle_forget()
        assert "Provide fact_id" in result

    def test_tool_description_prefers_fact_id(self):
        import inspect
        from null_memory.mcp import server as server_mod
        src = inspect.getsource(server_mod)
        assert "PREFER fact_id" in src
        assert "near-duplicates" in src
