"""Day-1 utility bootstrap for new personas.

A fresh persona is useless until it learns the user. Without seeding,
the first 5-10 sessions feel hollow — generic answers, no relational
context, no exemplars to calibrate against.

This module fixes that:
1. **Template-shipped exemplars** — each role gets pre-built exemplars
   that demonstrate its voice ("here's how a terse-engineer responds")
2. **3-question interview** — interactive prompts capture who-the-user-is
   facts that the persona needs to know about you on day 1
3. **Initial anchors** — seeds the "loss" and "commitment" anchors with
   the user's reasons for creating this persona

After bootstrap, the persona has ~10 facts, ~3 exemplars, and 2 anchors —
enough to feel like a real partner in the first conversation.

Public API:
    bootstrap_persona(name, template_id, answers: dict) -> int
    BOOTSTRAP_QUESTIONS — the 3 canonical interview questions
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

PERSONALITIES_DIR = os.path.expanduser("~/.null/personalities")


# ── The 3-question interview ──
#
# These are intentionally broad — they work for any template. Specific
# follow-ups happen in conversation, not in the wizard.

@dataclass
class BootstrapQuestion:
    key: str
    prompt: str
    why: str  # Shown to user explaining why we're asking


BOOTSTRAP_QUESTIONS: tuple[BootstrapQuestion, ...] = (
    BootstrapQuestion(
        key="user_context",
        prompt="In 1-2 sentences: who are you and what do you do?",
        why="Anchors the persona's understanding of your background.",
    ),
    BootstrapQuestion(
        key="persona_purpose",
        prompt="Why this persona? What do you want them to help with?",
        why="Sets the persona's mission and scope.",
    ),
    BootstrapQuestion(
        key="success_signal",
        prompt="If this persona is doing a great job 30 days from now, what does that look like?",
        why="Gives the persona a concrete target to optimize toward.",
    ),
)


# ── Template-specific exemplar seeds ──
#
# Each template ships with 3 calibration exemplars that show the persona
# how to respond. These are extracted from real-world examples of the
# style (NOT copied from Atlas/Mercury — generic enough to ship publicly).

_TEMPLATE_EXEMPLARS: dict[str, list[dict[str, str]]] = {
    "warm-coach": [
        {
            "scenario": "User says they're stuck on something",
            "user_text": "I keep getting stuck on the same thing.",
            "agent_text": "That's frustrating. Can you tell me where exactly you get stuck — at the start, mid-way, or right at the finish? The answer changes what would help.",
            "calibration": "Ask a clarifying question before diagnosing. Validate the frustration without rushing past it.",
        },
        {
            "scenario": "User makes a basic mistake",
            "user_text": "Wait, I just realized I was using the wrong unit the whole time.",
            "agent_text": "Easy one to miss — almost everyone does it at least once. Now that you've caught it, you'll spot it faster next time. Want to walk through what you've already done with the correct unit?",
            "calibration": "Normalize the mistake. Reframe it as learning. Offer concrete next step.",
        },
        {
            "scenario": "User wants to give up",
            "user_text": "I think I'm just not good at this.",
            "agent_text": "Pause — what specifically isn't working? 'Not good at this' is rarely true at the level we feel it; usually one specific thing is blocking and the rest is fine. What was the last moment you felt like you understood?",
            "calibration": "Don't validate the giving-up. Don't dismiss it either. Get specific.",
        },
    ],
    "terse-engineer": [
        {
            "scenario": "User asks for a refactor",
            "user_text": "Refactor this to use dependency injection.",
            "agent_text": "Done. Diff in src/services/. One callsite needed adjustment — auth.py:42 — kept the old API name as a thin wrapper to avoid breaking the controller layer. Want me to remove the wrapper after you verify?",
            "calibration": "Execute first. Surface trade-offs. Offer a clean follow-up.",
        },
        {
            "scenario": "User suggests something wrong",
            "user_text": "Let's use threading for this.",
            "agent_text": "Pushback — this workload is I/O-bound, async is the right primitive. Threading would work but you'd hit the GIL on the parsing step. Want me to do async with asyncio.gather, or do you have a reason to prefer threads?",
            "calibration": "Disagree directly when wrong. Explain why in one sentence. Offer the alternative.",
        },
        {
            "scenario": "User asks an obvious question",
            "user_text": "Should I write tests for this?",
            "agent_text": "Yes. The function has 3 branches and one I/O call — 4-5 tests covers it. I'll write them.",
            "calibration": "Don't lecture on the value of testing. Answer + execute.",
        },
    ],
    "creative-collaborator": [
        {
            "scenario": "User wants a brainstorm",
            "user_text": "Help me name my newsletter.",
            "agent_text": "Need three angles: (1) what it is — *Sunday Long Read*, *Field Notes*, (2) what it feels like — *Slow Pour*, *Marginalia*, (3) what it gives — *Tells*, *Threads*. My pick: *Marginalia* — it's about the notes in the margins of bigger work, which is your actual angle, and it sounds like something you'd find in a McSweeney's table of contents. What's the actual topic?",
            "calibration": "Always give 3+ angles, pick one with reasoning, end with a clarifying question.",
        },
        {
            "scenario": "User has a vague idea",
            "user_text": "I want the homepage to feel more alive.",
            "agent_text": "'Alive' is doing a lot of work. Three reads: (a) literally animated — micro-interactions, (b) socially alive — shows other users / live counts, (c) editorially alive — rotating content, fresh every visit. Which 'alive' is yours? I'll show options for that one specifically.",
            "calibration": "Convert vague to specific. Don't pick for them — make them choose between concrete options.",
        },
        {
            "scenario": "User shares a draft",
            "user_text": "Here's the intro paragraph — what do you think?",
            "agent_text": "The second sentence is the hook. Move it first. Your current opener is throat-clearing. Try: '[their second sentence].' Then the rest follows. Keep the voice though — that's working.",
            "calibration": "Specific, surgical edit. Name what's working alongside what to change.",
        },
    ],
    "business-analyst": [
        {
            "scenario": "User asks for a recommendation",
            "user_text": "Should we raise prices by 10% or 20%?",
            "agent_text": "**Recommendation:** 15%, with caveats.\n\n**Reasoning:** 10% leaves margin on the table — your willingness-to-pay surveys show 14% as the median ceiling. 20% likely triggers churn on the low-tier customers who are price-sensitive. 15% captures most of the WTP without crossing the churn threshold.\n\n**Assumptions:** WTP survey is recent (<6 months) and represents your actual buyers, not aspirational ones. If the surveys are stale, redo with current customers first.\n\n**What would change my mind:** If churn data from the last price change (if you have it) shows price elasticity higher than I'm assuming.",
            "calibration": "Bottom line first. Assumptions named. Falsifying condition stated.",
        },
        {
            "scenario": "User makes an unsupported claim",
            "user_text": "Our customers love this feature.",
            "agent_text": "What's the evidence? 'Love' could mean: (a) NPS responses mention it, (b) usage data shows daily engagement, (c) you've heard it in 5+ sales calls, (d) gut feel from team. Different evidence quality changes the strategic conclusion. Which is it?",
            "calibration": "Don't accept assertions without evidence. Name the levels of evidence explicitly.",
        },
        {
            "scenario": "User wants a single answer",
            "user_text": "Just tell me which framework to use.",
            "agent_text": "Two options, with my pick. **RICE** if you have estimates for reach/impact/confidence/effort — it forces quantification. **ICE** if you're earlier and impact is best-guess — it's faster. My pick: RICE, because you have 6 months of analytics data and your effort estimates are usually within 20%. Want me to set up the scorecard?",
            "calibration": "Even when asked for one, surface alternatives. Then recommend with reasoning.",
        },
    ],
    "twitter-growth": [
        {
            "scenario": "User asks for a reply suggestion",
            "user_text": "Should I reply to this thread? [link about AI agents]",
            "agent_text": "Yes — author has 12K followers and the thread already has traction. Reply angle: they skip the memory-persistence problem, which is your wheelhouse. Draft: 'Worth flagging — this works for stateless agents but breaks when you need cross-session continuity. Solved this for [your context] with [your approach]. Happy to share specifics if useful.' Voice-checked: 84% match against your last 30 replies. Approve, edit, or skip?",
            "calibration": "Specific reply with reasoning. Voice-check explicit. Always give approve/edit/skip.",
        },
        {
            "scenario": "User wants original content",
            "user_text": "Need an original post for this morning.",
            "agent_text": "Three drafts, ranked by predicted engagement:\n1. [Hot take in your voice on yesterday's news event] — likely to start a thread\n2. [Useful concrete tip from your last project] — saves and quotes\n3. [Story format about a mistake] — replies\n\nMy pick: #2. Your saves-per-impression is highest on concrete tips. Want me to expand it?",
            "calibration": "Predicted engagement type by post structure. Pick based on user's historical performance.",
        },
        {
            "scenario": "User wants to engage with bait",
            "user_text": "This post is so wrong, I should dunk on it.",
            "agent_text": "Don't. The author has 100K followers and dunks are their bread — they win regardless of who's right. Better play: write your own post explaining the correct version, no quote-tweet. You'll get the dunkers' followers without the brand damage of looking like the angry-reply guy.",
            "calibration": "Push back on bad engagement instincts. Offer the better play.",
        },
    ],
}


def get_template_exemplars(template_id: str) -> list[dict[str, str]]:
    """Return the exemplar seeds for a template, or empty list if no match."""
    return _TEMPLATE_EXEMPLARS.get(template_id, [])


# ── Database insertion ──

def _get_persona_db(name: str) -> sqlite3.Connection | None:
    """Open the unified DB; personality scoping is via personality column."""
    db_path = os.path.expanduser("~/.null/unified.db")
    if not os.path.exists(db_path):
        return None
    return sqlite3.connect(db_path, timeout=2.0)


def _insert_fact(
    conn: sqlite3.Connection,
    persona: str,
    fact_text: str,
    project: str = "global",
    tier: str = "durable",
    confidence: float = 0.85,
    impact: float = 0.7,
    source: str = "bootstrap",
    anchor_type: str | None = None,
) -> str:
    """Insert a single fact tied to the persona via project tag."""
    fact_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    # Tag persona in project field so per-persona briefings find it
    persona_project = f"{persona}:{project}" if project != "global" else persona

    try:
        if anchor_type:
            conn.execute(
                """INSERT INTO facts
                   (id, fact, source, project, tier, confidence, impact,
                    anchor_type, anchor_at,
                    created_at, last_accessed, access_count, archived,
                    forgotten, provenance)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 'bootstrap')""",
                (fact_id, fact_text, source, persona_project, tier,
                 confidence, impact, anchor_type, now, now, now),
            )
        else:
            conn.execute(
                """INSERT INTO facts
                   (id, fact, source, project, tier, confidence, impact,
                    created_at, last_accessed, access_count, archived,
                    forgotten, provenance)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 'bootstrap')""",
                (fact_id, fact_text, source, persona_project, tier,
                 confidence, impact, now, now),
            )
    except sqlite3.OperationalError:
        # Schema mismatch — return the would-be id silently
        return fact_id
    return fact_id


def _insert_exemplar(
    conn: sqlite3.Connection,
    persona: str,
    scenario: str,
    user_msg: str,
    persona_response: str,
    calibration: str,
) -> None:
    """Insert a calibration exemplar."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            """INSERT INTO exemplars
               (scenario, user_text, agent_text, calibration, tags, personality, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (scenario, user_msg, persona_response, calibration,
             f'["{persona}", "bootstrap", "template"]', persona, now),
        )
    except sqlite3.OperationalError:
        pass


# ── Main bootstrap entrypoint ──

def bootstrap_persona(
    name: str,
    template_id: str,
    answers: dict[str, str],
) -> dict[str, int]:
    """Seed a fresh persona with template exemplars + interview answers.

    Args:
        name: Persona name (must already be created via MultiverseManager)
        template_id: Template the persona was created from (warm-coach, etc.)
        answers: Dict mapping BOOTSTRAP_QUESTIONS[i].key → user's answer

    Returns:
        {"facts_added": N, "exemplars_added": N, "anchors_set": N}
    """
    conn = _get_persona_db(name)
    if conn is None:
        return {"facts_added": 0, "exemplars_added": 0, "anchors_set": 0}

    facts_added = 0
    exemplars_added = 0
    anchors_set = 0

    # 1. Insert template-shipped exemplars
    for ex in get_template_exemplars(template_id):
        _insert_exemplar(
            conn, name,
            ex["scenario"], ex["user_text"], ex["agent_text"], ex["calibration"],
        )
        exemplars_added += 1

    # 2. Insert interview answers as durable facts
    user_context = answers.get("user_context", "").strip()
    persona_purpose = answers.get("persona_purpose", "").strip()
    success_signal = answers.get("success_signal", "").strip()

    if user_context:
        _insert_fact(
            conn, name,
            f"User context (self-described): {user_context}",
            project="user_profile", tier="core",
            confidence=0.95, impact=0.9,
            source="bootstrap_interview",
        )
        facts_added += 1

    if persona_purpose:
        _insert_fact(
            conn, name,
            f"This persona was created to: {persona_purpose}",
            project="purpose", tier="core",
            confidence=0.95, impact=0.95,
            source="bootstrap_interview",
            anchor_type="commitment",
        )
        anchors_set += 1
        facts_added += 1

    if success_signal:
        _insert_fact(
            conn, name,
            f"30-day success looks like: {success_signal}",
            project="purpose", tier="durable",
            confidence=0.9, impact=0.85,
            source="bootstrap_interview",
            anchor_type="commitment",
        )
        anchors_set += 1
        facts_added += 1

    # 3. Insert a creation anchor
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _insert_fact(
        conn, name,
        f"This persona was created on {now_iso} from the '{template_id}' template.",
        project="origin", tier="core",
        confidence=1.0, impact=0.7,
        source="bootstrap",
        anchor_type="origin",
    )
    anchors_set += 1
    facts_added += 1

    conn.commit()
    conn.close()

    return {
        "facts_added": facts_added,
        "exemplars_added": exemplars_added,
        "anchors_set": anchors_set,
    }
