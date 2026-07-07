"""Null memory storage modules.

Cohesive pieces extracted from the agent.py god object (P2):

  * recall            — hybrid recall pipeline (RecallMixin)
  * viz               — Nebula projection + event emission (VizMixin)
  * probes            — calibration + continuity probes (ProbesMixin)
  * evaluation        — health/performance scoring (EvaluationMixin)
  * briefing_render   — morning-briefing formatting (BriefingRenderMixin)
  * maintenance       — gc/dedup/consolidate scheduling (MaintenanceMixin)
  * maintenance_actions — pure shared primitives for all maintenance engines
  * leader            — single leader-election implementation (LeaderLock)

The mixins compose into AgentMemory (null_memory.agent), which remains
the public facade — import sites elsewhere are unchanged.
"""
