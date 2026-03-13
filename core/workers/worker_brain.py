"""
Worker Brain — LLM Execution Engine for Workers
=================================================

Each worker gets its own mini "brain" that:
1. Checks pre-conditions (face seen, time range, etc.)
2. Builds a task-specific prompt with full tool access
3. Runs a multi-turn LLM loop (generate → tool calls → feed back → repeat)
4. Retries on failure with adjusted prompts
5. Reports results back to the WorkerManager

Uses generate_llm_resp.generate() — the same multi-provider router as Big Brain.
Non-streaming since workers are background tasks (no TTS needed).
"""

import asyncio
import json
import time
import subprocess
import concurrent.futures
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
import os
import sys
from core.workers.worker_engine import Worker, WorkerCondition
from paths import PROJECT_ROOT


# ============================================================================
# Face History Buffer (shared, populated by face_handler + main.py)
# ============================================================================

class FaceHistoryBuffer:
    """
    Thread-safe rolling buffer of face detection events.
    Populated by the face event listener, read by worker condition checks.
    """
    def __init__(self, max_entries: int = 200):
        self._events: List[Dict[str, Any]] = []
        self._max = max_entries
        import threading
        self._lock = threading.Lock()

    def record_face(self, person_name: str, event_type: str = "detected"):
        with self._lock:
            self._events.append({
                "person": person_name,
                "type": event_type,
                "timestamp": datetime.now().isoformat(),
                "epoch": time.time(),
            })
            if len(self._events) > self._max:
                self._events = self._events[-self._max:]

    def person_seen_within(self, person_name: str, within_minutes: int) -> bool:
        """Check if a person was seen within the last N minutes."""
        cutoff = time.time() - (within_minutes * 60)
        with self._lock:
            for event in reversed(self._events):
                if event["epoch"] < cutoff:
                    break
                if (event["person"].lower() == person_name.lower() and
                        event["type"] == "detected"):
                    return True
        return False

    def get_recent(self, minutes: int = 60) -> List[Dict[str, Any]]:
        """Get face events from the last N minutes."""
        cutoff = time.time() - (minutes * 60)
        with self._lock:
            return [e for e in self._events if e["epoch"] >= cutoff]


# Global singleton
_face_history: Optional[FaceHistoryBuffer] = None


def get_face_history() -> FaceHistoryBuffer:
    global _face_history
    if _face_history is None:
        _face_history = FaceHistoryBuffer()
    return _face_history


# ============================================================================
# Vision Context History (shared, populated by vision_handler)
# ============================================================================

class VisionContextHistory:
    """
    Rolling buffer of vision analysis results.
    Populated by VisionHandler after each vision update.
    """
    def __init__(self, max_entries: int = 50):
        self._history: List[Dict[str, Any]] = []
        self._max = max_entries
        import threading
        self._lock = threading.Lock()

    def record_vision(self, context: str):
        with self._lock:
            self._history.append({
                "context": context,
                "timestamp": datetime.now().isoformat(),
                "epoch": time.time(),
            })
            if len(self._history) > self._max:
                self._history = self._history[-self._max:]

    def get_recent(self, minutes: int = 60) -> List[Dict[str, Any]]:
        """Get vision contexts from the last N minutes."""
        cutoff = time.time() - (minutes * 60)
        with self._lock:
            return [v for v in self._history if v["epoch"] >= cutoff]

    def get_latest(self) -> Optional[str]:
        with self._lock:
            return self._history[-1]["context"] if self._history else None


_vision_history: Optional[VisionContextHistory] = None


def get_vision_history() -> VisionContextHistory:
    global _vision_history
    if _vision_history is None:
        _vision_history = VisionContextHistory()
    return _vision_history


# ============================================================================
# Condition Checker
# ============================================================================

def check_conditions(conditions: List[WorkerCondition]) -> tuple[bool, str]:
    """
    Check if all worker conditions are met.
    Returns (all_met: bool, reason: str).
    """
    if not conditions:
        return True, "No conditions to check"

    face_history = get_face_history()
    failed = []

    for cond in conditions:
        if cond.condition_type == "person_seen":
            person = cond.params.get("person", "")
            within = cond.params.get("within_minutes", 60)
            if not face_history.person_seen_within(person, within):
                failed.append(f"'{person}' not seen in the last {within} minutes")

        elif cond.condition_type == "time_range":
            start_hour = cond.params.get("start_hour", 0)
            end_hour = cond.params.get("end_hour", 24)
            current_hour = datetime.now().hour
            if not (start_hour <= current_hour < end_hour):
                failed.append(f"Current hour {current_hour} not in range [{start_hour}, {end_hour})")

        elif cond.condition_type == "custom":
            # Custom conditions are evaluated by the LLM itself
            pass

        else:
            print(f"[WorkerBrain] Unknown condition type: {cond.condition_type}")

    if failed:
        return False, "; ".join(failed)
    return True, "All conditions met"


# ============================================================================
# Worker Brain — LLM Execution
# ============================================================================

# Max LLM turns per worker execution (prevent infinite loops)
MAX_LLM_TURNS = 10


async def execute_worker(worker: Worker) -> tuple[bool, str, str | None]:
    """
    Execute a worker's task using the LLM with full tool access.
    
    This is the main entry point. It:
    1. Checks conditions
    2. Builds the LLM prompt with context
    3. Runs a multi-turn tool loop
    4. Returns (success, result_text, speak_text_or_None)
       - speak_text is the text to speak aloud via TTS (None = silent worker)
    """
    from core.brain.generate_llm_resp import generate as generate_llm
    from tools_and_config.tools import TOOLS, execute_tool

    print(f"\n{'=' * 50}")
    print(f"[WorkerBrain] Executing: {worker}")
    print(f"{'=' * 50}")

    # 1. Check pre-conditions
    conditions_met, condition_reason = check_conditions(worker.conditions)
    if not conditions_met:
        msg = f"Conditions not met: {condition_reason}"
        print(f"[WorkerBrain] {msg}")
        return False, msg, None

    print(f"[WorkerBrain] Conditions satisfied: {condition_reason}")

    # 2. Build context
    vision_history = get_vision_history()
    face_history = get_face_history()

    recent_vision = vision_history.get_recent(minutes=60)
    recent_faces = face_history.get_recent(minutes=60)

    vision_context = ""
    if recent_vision:
        vision_context = "\n".join([
            f"  [{v['timestamp']}] {v['context'][:200]}"
            for v in recent_vision[-5:]  # Last 5 vision snapshots
        ])

    face_context = ""
    if recent_faces:
        face_context = "\n".join([
            f"  [{f['timestamp']}] {f['person']} ({f['type']})"
            for f in recent_faces[-10:]  # Last 10 face events
        ])

    # Build tool descriptions for the prompt
    tool_desc = "\n".join([
        f"- {t['function']['name']}: {t['function']['description']}"
        for t in TOOLS
    ])
    # Add execute_python_code if available
    tool_desc += "\n- execute_python_code: Execute arbitrary Python code and return the output"

    current_time = datetime.now().strftime("%I:%M %p on %A, %B %d, %Y")

    worker_prompt = f"""You are Kiki's Worker Brain — an autonomous agent executing a specific task.
You have been scheduled to do this job and you MUST complete it.
You ARE Kiki — the witty, caring robot companion.

## YOUR TASK
{worker.task_description}

## CURRENT TIME
{current_time}

## AVAILABLE TOOLS
You can call ANY of these tools to complete your task. Return tool calls as JSON.
{tool_desc}

## RECENT VISION CONTEXT (What Kiki has seen recently)
{vision_context if vision_context else "No recent vision data available."}

## RECENT FACE DETECTIONS
{face_context if face_context else "No recent face detections."}

## INSTRUCTIONS
1. Analyze your task and decide what tools to call
2. If you need to call tools, respond with a JSON object:
   {{"tool_calls": [{{"tool": "tool_name", "args": {{"arg1": "value1"}}}}]}}
3. After receiving tool results, continue until your task is complete
4. When done, respond with a JSON object:
   {{"status": "completed", "summary": "What you accomplished", "speak": true/false, "speak_text": "What Kiki should say aloud (conversational, friendly, in Kiki's voice)"}}
   - Set "speak": true if the result is something Kiki should announce or say to the people around him
   - Set "speak_text" to what Kiki should SAY — make it sound like Kiki talking naturally, NOT a status report
   - Example speak_text: "Hey Vaibhav! I found some chill lo-fi beats based on your vibe. Playing it now!"
5. If you cannot complete the task, respond with:
   {{"status": "failed", "reason": "Why it failed", "speak": true/false, "speak_text": "optional message"}}

{f'## RETRY NOTE: This is retry #{worker.retry_count}. Previous attempt failed: {worker.last_result}. Try a different approach.' if worker.retry_count > 0 else ''}

Complete your task now."""

    # 3. Multi-turn LLM loop
    loop = asyncio.get_running_loop()
    conversation = [worker_prompt]
    final_result = ""

    for turn in range(MAX_LLM_TURNS):
        print(f"[WorkerBrain] LLM turn {turn + 1}/{MAX_LLM_TURNS}")

        # Call LLM
        prompt_text = "\n\n---\n\n".join(conversation)
        try:
            with concurrent.futures.ThreadPoolExecutor() as pool:
                response = await loop.run_in_executor(
                    pool,
                    lambda: generate_llm(prompt_text, thinking_level="HIGH", purpose="reasoning")
                )
        except Exception as e:
            print(f"[WorkerBrain] LLM call failed: {e}")
            return False, f"LLM error: {e}", None

        if not response:
            print("[WorkerBrain] LLM returned empty response")
            return False, "LLM returned empty response", None

        print(f"[WorkerBrain] LLM response: {response[:300]}...")

        # 4. Parse response
        try:
            # Try to extract JSON from response
            json_text = response
            if "```json" in json_text:
                json_text = json_text.split("```json")[1].split("```")[0]
            elif "```" in json_text:
                json_text = json_text.split("```")[1].split("```")[0]

            parsed = json.loads(json_text.strip())
        except (json.JSONDecodeError, IndexError) as _json_error:
            # If the response clearly looks like an attempt at JSON mapping (has tool_calls or status),
            # but failed to parse (usually due to unescaped quotes), feed the error back!
            if "status" in response or "tool_calls" in response or "{" in response:
                print(f"[WorkerBrain] JSON Decode Error. Feeding back to LLM to fix.")
                error_msg = f"Your response was invalid JSON. Ensure all quotes inside python code strings are properly escaped (use \\\" instead of \"). Error details: {_json_error}"
                conversation.append(f"SYSTEM ERROR:\n{error_msg}\n\nPlease output valid JSON.")
                continue
            
            # Response is plain text — treat as final result only if no JSON structures exist
            final_result = response
            print(f"[WorkerBrain] Non-JSON response, treating as completion")
            return True, final_result, None

        # Check if completed
        if parsed.get("status") == "completed":
            final_result = parsed.get("summary", "Task completed successfully")
            speak_text = parsed.get("speak_text") if parsed.get("speak") else None
            print(f"[WorkerBrain] Task completed: {final_result}")
            if speak_text:
                print(f"[WorkerBrain] Will speak: {speak_text[:100]}")
            return True, final_result, speak_text

        if parsed.get("status") == "failed":
            reason = parsed.get("reason", "Unknown failure")
            speak_text = parsed.get("speak_text") if parsed.get("speak") else None
            print(f"[WorkerBrain] Task failed: {reason}")
            return False, reason, speak_text

        # Handle tool calls
        tool_calls = parsed.get("tool_calls", [])
        if not tool_calls:
            # Completed without explicit status
            final_result = response
            return True, final_result, None

        # Execute tool calls
        tool_results = []
        for tc in tool_calls:
            tool_name = tc.get("tool", "")
            tool_args = tc.get("args", {})
            print(f"[WorkerBrain] Calling tool: {tool_name}({tool_args})")

            if tool_name == "execute_python_code":
                result = await _execute_python_code(tool_args.get("code", ""))
            else:
                try:
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        result = await loop.run_in_executor(
                            pool,
                            lambda tn=tool_name, ta=tool_args: execute_tool(tn, ta)
                        )
                except Exception as e:
                    result = f"Error: {e}"

            print(f"[WorkerBrain] Tool result:\n{str(result)}")
            tool_results.append({
                "tool": tool_name,
                "args": tool_args,
                "result": str(result)
            })

        # Feed tool results back into conversation
        results_text = "\n".join([
            f"Tool '{r['tool']}' returned: {r['result']}"
            for r in tool_results
        ])
        conversation.append(f"TOOL RESULTS:\n{results_text}\n\nContinue with your task. If done, respond with {{\"status\": \"completed\", \"summary\": \"...\"}}. If you need more tool calls, respond with {{\"tool_calls\": [...]}}.")

    # Exhausted all turns
    print(f"[WorkerBrain] Exhausted {MAX_LLM_TURNS} LLM turns")
    return False, f"Exhausted max LLM turns ({MAX_LLM_TURNS})", None


# ============================================================================
# Python Code Execution Tool
# ============================================================================

async def _execute_python_code(code: str, timeout: int = 30) -> str:
    """Execute Python code in a subprocess and return output."""
    if not code.strip():
        return "Error: No code provided"

    try:
        print(f"[WorkerBrain] Executing Python code ({len(code)} chars)...")
        loop = asyncio.get_running_loop()
        import tempfile
        import uuid
        
        # Write code to a temp file to avoid any command line quote escaping issues
        temp_dir = tempfile.gettempdir()
        filename = f"worker_script_{uuid.uuid4().hex[:8]}.py"
        file_path = os.path.join(temp_dir, filename)
        
        with open(file_path, "w") as f:
            f.write(code)

        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [sys.executable, file_path],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=str(PROJECT_ROOT)
                )
            )
            output = ""
            if result.stdout:
                output += result.stdout.strip()
            if result.stderr:
                output += f"\nSTDERR: {result.stderr.strip()}"
            return output if output else "Code executed with no output."
        except subprocess.TimeoutExpired:
            return f"Error: Code execution timed out after {timeout}s"
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
            
    except Exception as e:
        return f"Error executing code: {e}"
