"""Multiverse manager framework — the primitives users build on.

Null ships zero specific managers. This package defines the contract
every user-defined manager implements. Put your own manager at
``~/.null/personalities/<name>/manager.py`` with a class subclassing
``Manager``; Null's personality loader will discover and run it.

See docs/MANAGERS.md for the full authoring guide.
"""

from null_memory.managers.base import (
    Manager,
    Reasoner,
    ReasonerContext,
    ScoreResult,
    TickResult,
)
from null_memory.managers.reasoners import RuleReasoner

__all__ = [
    "Manager",
    "Reasoner",
    "ReasonerContext",
    "ScoreResult",
    "TickResult",
    "RuleReasoner",
]
