"""
Brain Module
============

Kiki's deeper thinking system — Big Brain analysis, knowledge base,
conversation summaries, and multi-provider LLM routing.

All operations run asynchronously and never block the fast voice pipeline.
"""

from core.brain.big_brain import analyze_conversation, get_suggestions_for_prompt, get_big_brain_config
from core.brain.knowledge_base import get_knowledge_base, get_knowledge_summary, save_knowledge_base

__all__ = [
    "analyze_conversation",
    "get_suggestions_for_prompt",
    "get_big_brain_config",
    "get_knowledge_base",
    "get_knowledge_summary",
    "save_knowledge_base",
]
