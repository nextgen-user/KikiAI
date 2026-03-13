"""
Big Brain Module
================

An intelligent background advisor that reviews the voice AI's responses
and generates suggestions to improve future interactions.

Runs ASYNCHRONOUSLY after each TTS response completes, never blocking
the fast voice pipeline.

Ported from KIKI-SMART — standalone, no LiveKit dependencies.
"""

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Any, Optional, Callable

from tools_and_config.config_loader import get_full_config
from core.brain.knowledge_base import get_knowledge_base, save_knowledge_base


# ============================================================================
# Configuration
# ============================================================================

def get_big_brain_config() -> Dict[str, Any]:
    """Get Big Brain configuration from config."""
    config = get_full_config()
    return config.get("big_brain", {
        "enabled": True,
        "min_conversation_length": 2,
        "max_suggestions_in_context": 3,
        "trigger_delay_seconds": 0.5,
        "skip_trivial_exchanges": True,
        "trivial_patterns": ["ok", "okay", "hmm", "hm", "uh huh", "yeah", "yes", "no", "bye"]
    })


# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class BigBrainSuggestions:
    """Structured suggestions from the Big Brain analysis."""

    response_quality: str = "good"
    quality_reasoning: str = ""
    friendship_suggestions: List[str] = field(default_factory=list)
    tool_suggestions: List[str] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    emotional_awareness: str = ""
    proactive_ideas: List[str] = field(default_factory=list)
    mood_suggestion: str = ""
    personality_notes: str = ""
    witty_additions: List[str] = field(default_factory=list)
    engagement_tips: List[str] = field(default_factory=list)
    knowledge_updates: List[Dict[str, str]] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_prompt_injection(self) -> str:
        """Format suggestions for system prompt injection."""
        lines = ["[Friend Advisor Notes - Consider these to be a better friend]"]

        if self.response_quality in ["too_assistant_like", "missed_opportunity"]:
            lines.append(f"• Previous: {self.response_quality} - {self.quality_reasoning}")
        if self.friendship_suggestions:
            lines.append(f"• Be a better friend: {'; '.join(self.friendship_suggestions)}")
        if self.tool_suggestions:
            lines.append(f"• Try using: {'; '.join(self.tool_suggestions)}")
        if self.emotional_awareness:
            lines.append(f"• Emotions: {self.emotional_awareness}")
        if self.proactive_ideas:
            lines.append(f"• Share: {'; '.join(self.proactive_ideas[:2])}")
        if self.mood_suggestion:
            lines.append(f"• Your mood: {self.mood_suggestion}")
        if self.personality_notes:
            lines.append(f"• Personality: {self.personality_notes}")
        if not self.friendship_suggestions and self.witty_additions:
            lines.append(f"• Try this wit: {self.witty_additions[0]}")

        return "\n".join(lines)

    def is_empty(self) -> bool:
        return (
            self.response_quality in ["good", "great_friend"] and
            not self.friendship_suggestions and
            not self.tool_suggestions and
            not self.proactive_ideas and
            not self.witty_additions and
            not self.engagement_tips and
            not self.personality_notes and
            not self.mood_suggestion
        )


# ============================================================================
# Suggestion Manager (Thread-Safe)
# ============================================================================

class BigBrainSuggestionManager:
    """Manages the rolling buffer of Big Brain suggestions."""

    def __init__(self, max_suggestions: int = 3):
        self._suggestions: List[BigBrainSuggestions] = []
        self._max = max_suggestions
        self._lock = asyncio.Lock()
        self._pending_analysis = False

    async def add_suggestion(self, suggestion: BigBrainSuggestions) -> None:
        async with self._lock:
            if not suggestion.is_empty():
                self._suggestions.append(suggestion)
                if len(self._suggestions) > self._max:
                    self._suggestions = self._suggestions[-self._max:]

    async def get_prompt_injection(self) -> Optional[str]:
        async with self._lock:
            if not self._suggestions:
                return None
            latest = self._suggestions[-1]
            return latest.to_prompt_injection()

    async def clear(self) -> None:
        async with self._lock:
            self._suggestions.clear()

    @property
    def is_analyzing(self) -> bool:
        return self._pending_analysis

    def set_analyzing(self, value: bool) -> None:
        self._pending_analysis = value


# ============================================================================
# Big Brain Analysis
# ============================================================================

_suggestion_manager: Optional[BigBrainSuggestionManager] = None


def get_suggestion_manager() -> BigBrainSuggestionManager:
    global _suggestion_manager
    if _suggestion_manager is None:
        config = get_big_brain_config()
        _suggestion_manager = BigBrainSuggestionManager(
            max_suggestions=config.get("max_suggestions_in_context", 3)
        )
    return _suggestion_manager


def _is_trivial_exchange(user_message: str) -> bool:
    config = get_big_brain_config()
    if not config.get("skip_trivial_exchanges", True):
        return False
    clean_msg = user_message.strip().lower()
    clean_msg = re.sub(r'[^\w\s]', '', clean_msg)
    trivial_patterns = config.get("trivial_patterns", [
        "ok", "okay", "hmm", "hm", "uh huh", "yeah", "yes", "no", "bye",
        "thanks", "thank you", "cool", "nice", "great", "alright"
    ])
    return clean_msg in trivial_patterns or len(clean_msg) < 3


def _build_analysis_prompt(
    conversation_history: List[Dict[str, str]],
    past_conversation_summary: str,
    knowledge_summary: str,
    available_tools: Dict[str, str],
    last_user_message: str,
    last_ai_response: str
) -> str:
    """Build the prompt for Big Brain analysis."""
    convo_text = ""
    for msg in conversation_history:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, str):
            convo_text += f"{role.upper()}: {content}\n"

    config = get_big_brain_config()

    tools_context = config.get("tools_context", None)
    if not tools_context:
        tools_text = ""
        if available_tools:
            for name, description in available_tools.items():
                clean_desc = " ".join(description.split()) if description else "No description"
                tools_text += f"- {name}: {clean_desc}\n"
        else:
            tools_text = "No tools available"
        tools_context = tools_text

    personality_context = config.get("personality_context",
        "Kiki is a sarcastic, TARS-style robot with 99% humor mode. He uses witty observations, dry humor, and maintains a fun but helpful personality.")

    prompt_template = config.get("analysis_prompt_template", None)

    if prompt_template:
        json_schema = config.get("json_schema", "{}")
        knowledge_examples = config.get("knowledge_examples", "")
        guidelines = config.get("guidelines", "")

        return prompt_template.format(
            personality_context=personality_context,
            tools_context=tools_context,
            past_conversation_summary=past_conversation_summary or "No past summary available.",
            knowledge_summary=knowledge_summary or "Empty knowledge base",
            convo_text=convo_text,
            last_user_message=last_user_message,
            last_ai_response=last_ai_response,
            json_schema=json_schema,
            knowledge_examples=knowledge_examples,
            guidelines=guidelines
        )
    else:
        return f"""You are an intelligent advisor reviewing a conversation between a witty robot voice assistant named "Kiki" and a human user.

PERSONALITY CONTEXT:
{personality_context}

AVAILABLE TOOLS:
{tools_context}

PAST CONVERSATION SUMMARY:
{past_conversation_summary or "No past summary available."}

KNOWLEDGE BASE SUMMARY:
{knowledge_summary or "Empty knowledge base"}

RECENT CONVERSATION:
{convo_text}

LAST EXCHANGE TO ANALYZE:
User: {last_user_message}
Kiki: {last_ai_response}

TASK: Analyze this exchange and provide suggestions to make Kiki better. Return a JSON object with these fields:

{{
    "response_quality": "good" | "could_improve" | "wrong",
    "quality_reasoning": "Brief explanation of quality assessment",
    "friendship_suggestions": ["How to be a better friend"],
    "tool_suggestions": ["List of tools Kiki should consider using"],
    "tool_calls": [],
    "emotional_awareness": "Did Kiki notice emotional cues?",
    "proactive_ideas": ["Things to bring up proactively"],
    "mood_suggestion": "What mood should Kiki be in?",
    "personality_notes": "Personality consistency notes",
    "witty_additions": ["Jokes/wit to add"],
    "engagement_tips": ["User engagement ideas"],
    "knowledge_updates": [
        {{
            "category": "learnings|people|environments|facts|personality|self",
            "key": "name_or_topic",
            "attribute": "optional: appearance|character|routine|interests|current_ongoing|notes|description",
            "value": "what to remember"
        }}
    ]
}}

GUIDELINES:
- Be constructive, not overly critical
- Focus on practical, actionable suggestions
- Keep suggestions SHORT - Kiki speaks responses aloud
- If the response was good, say so!
- knowledge_updates should capture user info, environment details, routines, preferences
- Only suggest knowledge updates for genuinely useful information

Return ONLY valid JSON, no markdown formatting."""


def _parse_suggestions(response_text: str) -> BigBrainSuggestions:
    """Parse the LLM response into a BigBrainSuggestions object."""
    try:
        json_text = response_text
        if "```json" in json_text:
            json_text = json_text.split("```json")[1].split("```")[0]
        elif "```" in json_text:
            json_text = json_text.split("```")[1].split("```")[0]

        data = json.loads(json_text.strip())

        return BigBrainSuggestions(
            response_quality=data.get("response_quality", "good"),
            quality_reasoning=data.get("quality_reasoning", ""),
            friendship_suggestions=data.get("friendship_suggestions", []),
            tool_suggestions=data.get("tool_suggestions", []),
            tool_calls=data.get("tool_calls", []),
            emotional_awareness=data.get("emotional_awareness", ""),
            proactive_ideas=data.get("proactive_ideas", []),
            mood_suggestion=data.get("mood_suggestion", ""),
            personality_notes=data.get("personality_notes", ""),
            witty_additions=data.get("witty_additions", []),
            engagement_tips=data.get("engagement_tips", []),
            knowledge_updates=data.get("knowledge_updates", [])
        )
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"[BigBrain] Error parsing response: {e}")
        print(f"[BigBrain] Raw response: {response_text[:500]}...")
        return BigBrainSuggestions()


async def _apply_knowledge_updates(updates: List[Dict[str, str]]) -> None:
    """Apply knowledge base updates from Big Brain suggestions."""
    if not updates:
        return

    kb = get_knowledge_base()
    applied_count = 0

    for update in updates:
        try:
            category = update.get("category", "")
            key = update.get("key", "")
            value = update.get("value", "")
            attribute = update.get("attribute", "")

            if not value:
                continue

            if category == "self":
                key = "Kiki"
                category = "people"

            if category == "learnings":
                if key:
                    kb.add_learning(key, value)
                    applied_count += 1
            elif category == "people":
                if not key:
                    continue
                if attribute == "appearance":
                    kb.add_person_attribute(key, "appearance", value)
                elif attribute == "character":
                    kb.set_person_character(key, value)
                elif attribute == "routine":
                    items = [v.strip() for v in value.split(",")]
                    for item in items:
                        kb.add_routine_item(key, item)
                elif attribute in ("interests", "hobbies"):
                    items = [v.strip() for v in value.split(",")]
                    for item in items:
                        kb.add_interest(key, item)
                elif attribute == "current_ongoing":
                    kb.set_current_ongoing(key, value)
                elif attribute == "notes":
                    kb.add_note_to_person(key, value)
                else:
                    kb.add_note_to_person(key, value)
                applied_count += 1
            elif category == "environments":
                if key:
                    kb.add_environment(key, description=value)
                    applied_count += 1
            elif category == "facts":
                if key:
                    kb.add_fact(key, value)
                    applied_count += 1
            elif category == "personality":
                if key:
                    kb.add_trait(key)
                    applied_count += 1
        except Exception as e:
            print(f"[BigBrain] Error applying knowledge update: {e}")

    if applied_count > 0:
        save_knowledge_base()
        print(f"[BigBrain] Applied {applied_count} knowledge updates")


# ============================================================================
# Tool Execution for Big Brain
# ============================================================================

def get_tool_execution_config() -> Dict[str, Any]:
    config = get_big_brain_config()
    return config.get("tool_execution", {
        "enabled": False,
        "allowed_tools": [],
        "blocked_tools": [],
        "max_tool_calls_per_analysis": 3,
        "tool_timeout_seconds": 15
    })


async def execute_tool_calls(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Execute the tool calls requested by Big Brain."""
    tool_config = get_tool_execution_config()

    if not tool_config.get("enabled", False):
        print("[BigBrain] Tool execution disabled in config")
        return []

    if not tool_calls:
        return []

    max_calls = tool_config.get("max_tool_calls_per_analysis", 3)
    timeout = tool_config.get("tool_timeout_seconds", 15)
    allowed_tool_names = tool_config.get("allowed_tools", [])

    tool_calls = tool_calls[:max_calls]
    results = []

    # Import tool executor
    from tools_and_config.tools import execute_tool_async

    for call in tool_calls:
        tool_name = call.get("tool", "")
        args = call.get("args", {})
        reason = call.get("reason", "")

        print(f"[BigBrain] Attempting to execute tool: {tool_name}")
        print(f"[BigBrain]   Reason: {reason}")
        print(f"[BigBrain]   Args: {args}")

        if allowed_tool_names and tool_name not in allowed_tool_names:
            print(f"[BigBrain] Tool '{tool_name}' not in allowed list, skipping")
            results.append({
                "tool": tool_name, "args": args,
                "result": f"Tool '{tool_name}' is not allowed for Big Brain execution",
                "success": False
            })
            continue

        try:
            result = await asyncio.wait_for(
                execute_tool_async(tool_name, args),
                timeout=timeout
            )
            print(f"[BigBrain] Tool '{tool_name}' result: {str(result)[:200]}...")
            results.append({
                "tool": tool_name, "args": args,
                "result": str(result), "success": True
            })
        except asyncio.TimeoutError:
            error_msg = f"Tool '{tool_name}' timed out after {timeout}s"
            print(f"[BigBrain] {error_msg}")
            results.append({
                "tool": tool_name, "args": args,
                "result": error_msg, "success": False
            })
        except Exception as e:
            error_msg = f"Error executing '{tool_name}': {str(e)}"
            print(f"[BigBrain] {error_msg}")
            results.append({
                "tool": tool_name, "args": args,
                "result": error_msg, "success": False
            })

    return results


def _build_tool_results_followup_prompt(
    original_prompt: str,
    original_response: str,
    tool_results: List[Dict[str, Any]]
) -> str:
    results_text = "\n".join([
        f"- {r['tool']}({r['args']}): {'SUCCESS' if r['success'] else 'FAILED'}\n  Result: {r['result'][:500]}"
        for r in tool_results
    ])

    return f"""{original_prompt}

## YOUR PREVIOUS RESPONSE (with tool_calls)
{original_response[:2000]}

## TOOL EXECUTION RESULTS
The tools you requested have been executed. Here are the results:

{results_text}

## FINAL ANALYSIS
Now that you have the tool results, provide your FINAL analysis.
Update your suggestions based on the actual data you received.

Return the SAME JSON format, but with tool_calls set to an empty array.
Return ONLY valid JSON, no markdown formatting."""


async def analyze_conversation(
    conversation_history: List[Dict[str, str]],
    past_conversation_summary: str,
    knowledge_summary: str,
    available_tools: Dict[str, str],
    last_user_message: str,
    last_ai_response: str
) -> Optional[BigBrainSuggestions]:
    """
    Analyze a conversation turn and generate suggestions.
    Runs ASYNCHRONOUSLY — never blocks the main conversation.
    """
    config = get_big_brain_config()
    full_config = get_full_config()

    if not config.get("enabled", True):
        print("[BigBrain] Disabled in config")
        return None

    if _is_trivial_exchange(last_user_message):
        print(f"[BigBrain] Skipping trivial exchange: '{last_user_message[:30]}...'")
        return None

    min_length = config.get("min_conversation_length", 2)
    if len(conversation_history) < min_length:
        print(f"[BigBrain] Conversation too short ({len(conversation_history)} < {min_length})")
        return None

    manager = get_suggestion_manager()
    manager.set_analyzing(True)

    try:
        from core.brain.generate_llm_resp import generate

        prompt = _build_analysis_prompt(
            conversation_history,
            past_conversation_summary,
            knowledge_summary,
            available_tools,
            last_user_message,
            last_ai_response
        )

        delay = config.get("trigger_delay_seconds", 0.5)
        if delay > 0:
            await asyncio.sleep(delay)

        print("[BigBrain] Analyzing conversation with HIGH thinking...")

        import concurrent.futures
        loop = asyncio.get_running_loop()

        with concurrent.futures.ThreadPoolExecutor() as pool:
            # --- Brain Vision Injection ---
            brain_b64_image = None
            vi_cfg = full_config.get("vision_injection", {})
            brain_vi_cfg = vi_cfg.get("brain_llm", {})
            if vi_cfg.get("enabled", False) and brain_vi_cfg.get("enabled", False):
                brain_n = brain_vi_cfg.get("every_n_turns", 3)
                if not hasattr(analyze_conversation, '_brain_turn_counter'):
                    analyze_conversation._brain_turn_counter = 0
                analyze_conversation._brain_turn_counter += 1
                if analyze_conversation._brain_turn_counter % brain_n == 0:
                    try:
                        from core.vision.camera import capture_photo_b64
                        brain_b64_image = await loop.run_in_executor(pool, capture_photo_b64)
                        if brain_b64_image:
                            print(f"[BigBrain] Injected camera image into brain analysis (turn {analyze_conversation._brain_turn_counter})")
                        else:
                            print(f"[BigBrain] Camera capture failed for brain vision injection")
                    except Exception as e:
                        print(f"[BigBrain] Error capturing image for brain: {e}")

            response = await loop.run_in_executor(
                pool,
                lambda: generate(prompt, b64_image=brain_b64_image, thinking_level="HIGH", purpose="reasoning")
            )

        print(f"[BigBrain] Analysis complete, parsing suggestions...")

        suggestions = _parse_suggestions(response)

        # Handle tool execution if requested
        if suggestions.tool_calls:
            print(f"[BigBrain] Big Brain requested {len(suggestions.tool_calls)} tool calls")
            tool_results = await execute_tool_calls(suggestions.tool_calls)

            if tool_results:
                print(f"[BigBrain] Executed {len(tool_results)} tools, sending results back...")
                followup_prompt = _build_tool_results_followup_prompt(
                    prompt, response, tool_results
                )
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    final_response = await loop.run_in_executor(
                        pool,
                        lambda: generate(followup_prompt, thinking_level="HIGH", purpose="reasoning")
                    )
                print(f"[BigBrain] Final analysis with tool results complete")
                suggestions = _parse_suggestions(final_response)

        # Apply knowledge updates
        await _apply_knowledge_updates(suggestions.knowledge_updates)

        # Store suggestions
        await manager.add_suggestion(suggestions)

        print(f"[BigBrain] Generated suggestions: quality={suggestions.response_quality}")
        if suggestions.friendship_suggestions:
            print(f"[BigBrain] Friendship tips: {suggestions.friendship_suggestions}")
        if suggestions.tool_suggestions:
            print(f"[BigBrain] Tool suggestions: {suggestions.tool_suggestions}")
        if suggestions.mood_suggestion:
            print(f"[BigBrain] Mood: {suggestions.mood_suggestion}")
        if suggestions.proactive_ideas:
            print(f"[BigBrain] Proactive ideas: {suggestions.proactive_ideas}")

        return suggestions

    except Exception as e:
        print(f"[BigBrain] Error during analysis: {e}")
        return None
    finally:
        manager.set_analyzing(False)


async def get_suggestions_for_prompt() -> Optional[str]:
    """Get formatted suggestions to inject into the next prompt."""
    manager = get_suggestion_manager()
    return await manager.get_prompt_injection()
