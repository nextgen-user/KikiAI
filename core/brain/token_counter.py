"""
Token Counter
=============

Provides token counting for chat context management.
Uses tiktoken when available, falls back to character-based estimation.

Ported from KIKI-SMART — standalone, no LiveKit dependencies.
"""

from typing import Any, List, Dict


# ============================================================================
# Module Initialization
# ============================================================================

try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    print("[TokenCounter] tiktoken not available, using character-based estimation")


# ============================================================================
# Constants
# ============================================================================

DEFAULT_MODEL = "gpt-4"
CHARS_PER_TOKEN_ESTIMATE = 4  # Approximate for English text


# ============================================================================
# Public Functions
# ============================================================================

def count_tokens(messages: List[Dict[str, str]], model: str = DEFAULT_MODEL) -> int:
    """
    Count tokens in chat message history.
    
    Works with KikiFast's message format: [{"role": "...", "content": "..."}]
    Uses tiktoken if available, otherwise character-based estimation.
    """
    try:
        total = 0
        
        for msg in messages:
            text_content = _extract_text(msg)
            
            if TIKTOKEN_AVAILABLE:
                total += _count_with_tiktoken(text_content, model)
            else:
                total += _count_with_estimation(text_content)
        
        return total
        
    except Exception as e:
        print(f"[TokenCounter] Error: {e}")
        return 0


def is_tiktoken_available() -> bool:
    """Check if tiktoken is available for accurate token counting."""
    return TIKTOKEN_AVAILABLE


# ============================================================================
# Private Helper Functions
# ============================================================================

def _extract_text(msg: Any) -> str:
    """
    Extract text content from a chat message.
    
    Supports both dict format {"role": ..., "content": ...} 
    and object format with .role/.content attributes.
    """
    text_content = ""
    
    # Dict format (KikiFast style)
    if isinstance(msg, dict):
        content = msg.get("content", "")
        if isinstance(content, str):
            text_content += content
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    text_content += item
                elif isinstance(item, dict) and "text" in item:
                    text_content += item["text"]
        role = msg.get("role", "")
        text_content += role
    # Object format (LiveKit style — kept for compatibility)
    elif hasattr(msg, 'content'):
        content = msg.content
        if isinstance(content, str):
            text_content += content
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    text_content += item
                elif hasattr(item, 'text'):
                    text_content += item.text
        if hasattr(msg, 'role'):
            text_content += msg.role
    
    return text_content


def _count_with_tiktoken(text: str, model: str) -> int:
    """Count tokens using tiktoken library."""
    enc = tiktoken.encoding_for_model(model)
    return len(enc.encode(text, disallowed_special=()))


def _count_with_estimation(text: str) -> int:
    """Estimate token count using character-based heuristic."""
    return len(text) // CHARS_PER_TOKEN_ESTIMATE
