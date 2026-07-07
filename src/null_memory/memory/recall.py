"""Recall pipeline — hybrid search over facts, mistakes, anchors.

Extracted from agent.py (P2 god-object split). Contains:
  * the word-expansion thesaurus + stop-word/tokenization helpers
  * RecallMixin.recall() — Reciprocal-Rank-Fusion hybrid retrieval
    (BM25 + trigram + semantic candidate lists fused by rank, with
    confidence/impact/anchor as bounded priors), then mistakes,
    relationship graph, and context expansion.

Ranking design (P1-10 / N6)
===========================
BM25 log-odds, trigram match counts, and cosine similarity live in
incommensurable units — multiplying them together (the old approach)
produced erratic orderings that needed three layers of anchor-promotion
hacks to patch over. RRF fuses the lists by *rank* only:

    rrf(d) = Σ_lists  w_list / (RRF_K + rank_list(d))

so a fact that appears near the top of any list scores well, and a fact
confirmed by multiple lists scores best. Confidence, impact, and anchor
status are applied afterwards as BOUNDED multiplicative priors — each
can nudge but never bury a strong relevance signal, which is why the
slot-#2 anchor promotion hack and the Stage-3c anchor rerank are gone.

Mixed into AgentMemory; methods rely on the host's db / embeddings /
effective_confidence / _emit_nebula_event attributes.
"""

from __future__ import annotations

import os

# RRF constant — standard value from the literature; large enough that
# rank-1 vs rank-2 differences don't dominate, small enough that depth
# in a list still matters.
RRF_K = 60.0

# Per-list weights. Trigram is a fuzzy fallback — useful for catching
# substring/typo matches but noisier than BM25 or semantic, so it
# contributes at reduced weight.
RRF_LIST_WEIGHTS = {
    "bm25": 1.0,
    "trigram": 0.6,
    "semantic": 1.0,
    "mistake": 1.0,
}

# Bounded prior: anchored (load-bearing) memories get a fixed edge over
# equally-relevant plain facts, replacing the old 2.0x multiplier + the
# slot-#2 promotion + Stage 3c rerank.
ANCHOR_PRIOR = 1.5

# Minimum cosine for a pure-semantic candidate to enter the fusion.
SEMANTIC_FLOOR = 0.35

# Near-tie guard for destructive fuzzy matches (forget). Two facts that
# shadow each other in every candidate list at adjacent ranks score
# (RRF_K + r) / (RRF_K + r + 1) ≈ 61/62 ≈ 0.984 of each other — that is
# the signature of a near-duplicate (the incident class: 90% text
# overlap, wrong fact soft-deleted). A runner-up that drops out of one
# full-weight list lands at ≤ ~0.76 of the top score. 0.9 splits the
# gap: near-duplicates refuse, clear winners proceed.
FORGET_NEAR_TIE_RATIO = 0.9

# ── Word Expansion Thesaurus ──
# Maps a concept to related terms so "database" finds "Postgres", etc.
WORD_EXPANSION: dict[str, set[str]] = {
    "database": {"postgres", "redis", "sql", "schema", "migration", "sqlite", "mysql", "mongo", "dynamo", "supabase", "neon"},
    "trading": {"order", "fill", "position", "price", "arbitrage", "exchange", "clob", "polymarket", "strategy", "pnl"},
    "web": {"html", "css", "react", "next", "api", "endpoint", "frontend", "backend", "route", "page"},
    "test": {"pytest", "vitest", "assert", "mock", "coverage", "spec", "fixture", "unittest"},
    "auth": {"login", "token", "jwt", "oauth", "session", "password", "credential", "permission"},
    "error": {"bug", "crash", "exception", "panic", "fix", "fail", "broken", "issue", "debug"},
    "memory": {"context", "session", "recall", "persist", "cache", "knowledge", "fact", "null"},
    "compress": {"token", "reduction", "symbol", "aleph", "salience", "summary", "compact"},
    "parse": {"ast", "tree-sitter", "syntax", "node", "grammar", "extract", "token"},
    "deploy": {"ci", "cd", "pipeline", "docker", "vercel", "netlify", "release", "ship"},
    "config": {"env", "settings", "toml", "yaml", "json", "dotenv", "pyproject"},
    "type": {"typescript", "mypy", "annotation", "interface", "schema", "pydantic", "dataclass"},
    "async": {"await", "promise", "future", "concurrent", "parallel", "thread", "coroutine"},
    "crypto": {"bitcoin", "ethereum", "blockchain", "wallet", "defi", "nft", "web3"},
    "ml": {"model", "training", "inference", "embedding", "vector", "neural", "llm", "ai"},
    "file": {"path", "directory", "read", "write", "fs", "io", "stream", "glob"},
    "git": {"commit", "branch", "merge", "rebase", "push", "pull", "pr", "diff"},
    "python": {"pip", "venv", "poetry", "setuptools", "pypi", "wheel", "conda"},
    "rust": {"cargo", "crate", "unsafe", "borrow", "lifetime", "trait", "impl"},
    "javascript": {"node", "npm", "deno", "bun", "es6", "commonjs", "esm", "typescript"},
    "go": {"goroutine", "channel", "module", "gomod", "interface", "struct"},
    "license": {"key", "ed25519", "signing", "validation", "trial", "subscription", "stripe"},
    "mcp": {"tool", "server", "handler", "protocol", "cursor", "claude", "windsurf"},
    "user": {"pete", "preference", "feedback", "identity", "style", "pattern"},
    "performance": {"speed", "benchmark", "latency", "throughput", "cache", "optimize", "fast"},
    "security": {"xss", "injection", "csrf", "sanitize", "escape", "vulnerability"},
    "network": {"http", "request", "response", "socket", "websocket", "grpc", "rest"},
    "ui": {"component", "button", "modal", "layout", "tailwind", "css", "responsive"},
    "state": {"redux", "zustand", "context", "store", "atom", "signal", "reactive"},
}

# Build reverse index: term → set of expansion groups it belongs to
_REVERSE_EXPANSION: dict[str, set[str]] = {}
for _concept, _terms in WORD_EXPANSION.items():
    for _term in _terms:
        _REVERSE_EXPANSION.setdefault(_term, set()).add(_concept)
    _REVERSE_EXPANSION.setdefault(_concept, set()).update(_terms)


_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "to", "for",
    "in", "on", "of", "and", "or", "not", "with", "from", "by",
    "i", "it", "that", "this", "be", "at", "do", "did", "has",
    "had", "have", "my", "me", "we", "us", "so", "if", "but",
})


def _strip_punctuation(word: str) -> str:
    """Strip leading/trailing punctuation from a word."""
    return word.strip(".,;:!?\"'()[]{}—–-/\\")


def _expand_tokens(tokens: list[str]) -> set[str]:
    """Expand query tokens using the thesaurus. Returns original + expanded tokens."""
    expanded = set(tokens)
    for token in tokens:
        # Direct concept lookup
        if token in WORD_EXPANSION:
            expanded.update(WORD_EXPANSION[token])
        # Reverse lookup: token is a member of some concept group
        if token in _REVERSE_EXPANSION:
            expanded.update(_REVERSE_EXPANSION[token])
    return expanded


class RecallMixin:
    """Recall pipeline methods for AgentMemory."""

    def _passes_recall_filters(self, row: dict, proj: str | None,
                               include_archived: bool,
                               since_str: str | None,
                               session_id: str | None) -> bool:
        """Shared row filter for candidates fetched outside fts_search."""
        if row.get("forgotten") or row.get("superseded_by"):
            return False
        if not include_archived and row.get("archived"):
            return False
        if proj and row.get("project", "global") not in (proj, "global"):
            return False
        if since_str and row.get("created_at", "") <= since_str:
            return False
        if session_id and row.get("session_id") != session_id:
            return False
        return True

    def _recall_priors(self, row: dict) -> float:
        """Bounded multiplicative priors: confidence, impact, anchor.

        Each factor is bounded so it can shade the RRF relevance signal
        but never bury it:
          confidence -> [0.5, 1.0]
          impact     -> [0.8, 1.0]
          anchor     -> {1.0, ANCHOR_PRIOR}
        """
        eff_conf = self.effective_confidence(row)
        impact = row.get("impact") or 0.5
        prior = (0.5 + 0.5 * eff_conf) * (0.8 + 0.2 * impact)
        if row.get("anchor_type"):
            prior *= ANCHOR_PRIOR
        return prior

    def recall(self, query: str, project: str | None = None,
               limit: int = 10, include_mistakes: bool = True,
               include_archived: bool = False,
               since: str | None = None,
               session_id: str | None = None,
               _emit_event: bool = True) -> list[dict]:
        """Search knowledge + mistakes via Reciprocal Rank Fusion.

        Candidate lists (each internally ranked):
        1. BM25 keyword search (with thesaurus expansion) — wide net
        2. Trigram fuzzy match — catches substrings and typos
        3. Pure semantic search — conceptual matches (when embeddings available)

        Lists are fused by rank (see module docstring), then bounded
        confidence/impact/anchor priors are applied.
        """
        if not query or not query.strip():
            return []

        _trace = os.environ.get("NULL_TRACE_RECALL") == "1"

        # Parse temporal filter
        since_dt = self._parse_since(since) if since else None
        since_str = since_dt.isoformat() if since_dt else None

        # Normalize project
        proj = project.strip().lower() if project else None

        # Build expanded FTS query using thesaurus
        query_lower = query.lower()
        raw_tokens = [_strip_punctuation(t) for t in query_lower.split() if _strip_punctuation(t)]
        tokens = [t for t in raw_tokens if t not in _STOP_WORDS]
        if not tokens:
            tokens = raw_tokens
        if not tokens:
            return []

        # Expand tokens and build OR query for FTS5
        expanded = _expand_tokens(tokens)
        fts_terms = list(expanded)
        fts_query = " OR ".join(fts_terms)

        # Candidate pool: fact_id -> row; ranked lists: name -> [fact_id, ...]
        row_map: dict[str, dict] = {}
        ranked_lists: dict[str, list[str]] = {}

        # ── List 1: BM25 keyword search (already ranked by FTS5) ──
        fts_results = self.db.fts_search(
            fts_query, project=proj,
            include_archived=include_archived,
            since=since_str,
            limit=limit * 3,
        )
        bm25_list: list[str] = []
        for row in fts_results:
            if session_id and row.get("session_id") != session_id:
                continue
            fid = row["id"]
            row_map.setdefault(fid, dict(row))
            bm25_list.append(fid)
        ranked_lists["bm25"] = bm25_list

        # ── List 2: Trigram fuzzy match ──
        # Per-token searches merged by best (lowest) rank position.
        tri_best_rank: dict[str, int] = {}
        for token in tokens[:3]:
            trigram_results = self.db.trigram_search(
                token, project=proj, exclude_ids=set(), limit=limit * 2,
            )
            for rank, row in enumerate(trigram_results):
                if session_id and row.get("session_id") != session_id:
                    continue
                fid = row["id"]
                row_map.setdefault(fid, dict(row))
                if fid not in tri_best_rank or rank < tri_best_rank[fid]:
                    tri_best_rank[fid] = rank
        ranked_lists["trigram"] = [
            fid for fid, _ in sorted(tri_best_rank.items(), key=lambda kv: kv[1])
        ]

        # ── List 3: Pure semantic search (when embeddings available) ──
        emb = self.embeddings
        semantic_worthy = len(tokens) >= 2 or (len(tokens) == 1 and len(tokens[0]) >= 4)
        semantic_list: list[str] = []
        if emb is not None and semantic_worthy:
            try:
                semantic_results = emb.semantic_search(query, limit=limit * 3)
                for fid, sim in semantic_results:
                    if sim < SEMANTIC_FLOOR:
                        break  # results are sorted by similarity desc
                    if fid.startswith(("m_", "d_")):
                        continue  # mistake/decision embeddings, not facts
                    if fid not in row_map:
                        fact_row = self.db.get_fact_by_id(fid)
                        if fact_row is None:
                            continue
                        if not self._passes_recall_filters(
                                fact_row, proj, include_archived,
                                since_str, session_id):
                            continue
                        row_map[fid] = dict(fact_row)
                    semantic_list.append(fid)
            except Exception as e:
                # Embedding failure shouldn't break recall — but it must
                # be counted, or semantic recall degrades invisibly.
                self._note_embed_failure("recall.semantic", e)
        ranked_lists["semantic"] = semantic_list

        # ── Fusion: RRF over the candidate lists ──
        rrf_scores: dict[str, float] = {}
        for list_name, fid_list in ranked_lists.items():
            weight = RRF_LIST_WEIGHTS[list_name]
            for rank, fid in enumerate(fid_list):
                rrf_scores[fid] = rrf_scores.get(fid, 0.0) + weight / (RRF_K + rank + 1)

        scored: list[tuple[float, dict]] = []
        for fid, rrf in rrf_scores.items():
            row = row_map[fid]
            entry = dict(row)
            entry["_type"] = "fact"
            scored.append((rrf * self._recall_priors(row), entry))

        if _trace:
            print(f"[trace] lists: bm25={len(bm25_list)} "
                  f"trigram={len(ranked_lists['trigram'])} "
                  f"semantic={len(semantic_list)} fused={len(scored)}",
                  flush=True)
            for s, e in sorted(scored, key=lambda x: -x[0])[:5]:
                print(f"[trace]   {s:.5f} {'[A]' if e.get('anchor_type') else '   '} "
                      f"{e.get('fact', '')[:80]}", flush=True)

        # ── Mistakes: their own ranked list, fused at the same scale ──
        if include_mistakes:
            mistake_results = self.db.search_mistakes(query, project=proj, limit=5)
            weight = RRF_LIST_WEIGHTS["mistake"]
            for rank, m in enumerate(mistake_results):
                result_entry = dict(m)
                result_entry["_type"] = "mistake"
                scored.append((weight / (RRF_K + rank + 1), result_entry))

        # ── Relationship graph — boost + pull in related facts ──
        found_ids = {r["id"] for _, r in scored if r.get("_type") == "fact" and r.get("id")}
        if found_ids:
            # Collect all related IDs from matched facts
            related_ids: set[str] = set()
            for mid in found_ids:
                related_ids.update(self.db.get_related_ids(mid))

            if related_ids:
                # Boost already-found related facts (bounded)
                boosted = []
                for score, entry in scored:
                    eid = entry.get("id")
                    if eid and eid in related_ids:
                        boosted.append((score * 1.2, entry))
                    else:
                        boosted.append((score, entry))
                scored = boosted

                # Pull in related facts NOT already in results.
                # These are associative recalls — triggered by connection, not keywords
                new_related = related_ids - found_ids
                if new_related and len(scored) < limit * 2:
                    # Use the top matched fact's score as anchor
                    top_score = max(s for s, _ in scored) if scored else 1.0
                    for rid in list(new_related)[:limit]:
                        fact_row = self.db.get_fact_by_id(rid)
                        if fact_row is None:
                            continue
                        if not self._passes_recall_filters(
                                fact_row, proj, include_archived,
                                since_str, session_id):
                            continue
                        # Bounded associative score: fraction of the top
                        # score, shaded by the fact's own priors.
                        assoc_score = top_score * 0.4 * self._recall_priors(fact_row)
                        result_entry = dict(fact_row)
                        result_entry["_type"] = "fact"
                        scored.append((assoc_score, result_entry))

        # Sort by fused score descending, return top limit
        scored.sort(key=lambda x: -x[0])
        top = scored[:limit]
        results = []
        for score, entry in top:
            # Expose the fused score so destructive callers (forget) can
            # detect near-ties between the top candidates. Context
            # entries added below intentionally carry no _score.
            entry["_score"] = score
            results.append(entry)

        # ── Stage 6: Context expansion — add session neighbors for top facts ──
        result_ids = {e.get("id") for e in results if e.get("id")}
        context_entries: list[dict] = []
        for entry in results[:3]:  # Expand top 3 results only
            if entry.get("_type") != "fact":
                continue
            sid = entry.get("session_id")
            fid = entry.get("id")
            if not sid or not fid:
                continue
            neighbors = self.db.get_session_neighbors(fid, sid, n=2)
            for nb in neighbors:
                if nb["id"] not in result_ids:
                    nb["_type"] = "context"
                    nb["_context_of"] = fid
                    context_entries.append(nb)
                    result_ids.add(nb["id"])

        # Interleave context after their parent facts
        if context_entries:
            expanded = []
            for entry in results:
                expanded.append(entry)
                fid = entry.get("id")
                if fid:
                    for ctx in context_entries:
                        if ctx.get("_context_of") == fid:
                            expanded.append(ctx)
            results = expanded[:limit * 2]  # Cap total output

        # Track access on returned fact results and record for relationship linking
        accessed_ids = [e["id"] for e in results
                        if e.get("_type") in ("fact", "context") and e.get("id")]
        for fid in accessed_ids:
            if fid not in self._session_recalled_ids:
                self._session_recalled_ids.append(fid)
        # Keep bounded — only last 50 recalled IDs
        if len(self._session_recalled_ids) > 50:
            self._session_recalled_ids = self._session_recalled_ids[-50:]
        # ONE batched UPDATE + commit per recall (P0-6) — per-row writes
        # amplified two-instance write contention.
        self.db.update_facts_access_batch(accessed_ids)
        self.db.conn.commit()

        # Phase 3b S2: emit nebula event so the galaxy lights up.
        # Suppressed for internal loop callers (probe runs, briefing recall-quality
        # scans, anchor lookups) that would otherwise hammer nebula_events.
        if _emit_event:
            fact_ids = [e["id"] for e in results
                        if e.get("_type") in ("fact", "context") and e.get("id")]
            if fact_ids:
                primary = fact_ids[0]
                related = fact_ids[1:]
                self._emit_nebula_event(
                    kind="recall", fact_id=primary, related_ids=related,
                )

        return results

    def _reload_knowledge(self) -> None:
        """No-op — SQLite is always fresh."""
        pass

    def _mark_knowledge_dirty(self) -> None:
        """No-op — SQLite handles persistence."""
        pass
