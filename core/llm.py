"""
LLM response generation module for KikiFast voice assistant.
Streams responses via LiteLLM and yields complete sentences as they form.

Everything that can be pre-initialized is done at import time for minimum latency.
"""

import os
import json
import re
import time
import threading
import queue

# --- Pre-initialize at import time ---
# 1. Load Vertex AI credentials from environment (if available)
vertex_credentials_json = os.getenv("VERTEX_CREDENTIALS_JSON")
if not vertex_credentials_json:
    # Try loading from file path specified by GOOGLE_APPLICATION_CREDENTIALS
    _gcloud_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if _gcloud_path and os.path.exists(_gcloud_path):
        try:
            with open(_gcloud_path, 'r') as _f:
                vertex_credentials_json = json.dumps(json.load(_f))
        except Exception as _e:
            print(f"[LLM] Warning: Could not load Vertex credentials from {_gcloud_path}: {_e}")
    # else: vertex_credentials_json stays None — Vertex AI models won't work, but that's fine

# 2. Import litellm (triggers internal setup)
from litellm import completion

# 3. Cache config at module level (read once)
from tools_and_config.config_loader import get_llm_config
_LLM_CFG = get_llm_config()
_MODEL = _LLM_CFG["model"]
_FALLBACK_MODEL = _LLM_CFG.get("fallback_model")
_FALLBACK_TIMEOUT = _LLM_CFG.get("fallback_timeout", 5)
_TEMPERATURE = _LLM_CFG.get("temperature", 1.0)
_REASONING_EFFORT = _LLM_CFG.get("reasoning_effort", "low")

# 4. Import tools once
from tools_and_config.tools import TOOLS, execute_tool

# Sentence boundary pattern: split after . ! ? followed by a space or end-of-string
_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')

print(f"[LLM] Pre-initialized: model={_MODEL}, temp={_TEMPERATURE}")


def _extract_sentences(buffer):
    """
    Extract complete sentences from the buffer.
    Returns (list_of_complete_sentences, remaining_buffer).
    """
    parts = _SENTENCE_RE.split(buffer)
    if len(parts) <= 1:
        return [], buffer

    complete = parts[:-1]
    remaining = parts[-1]
    return complete, remaining


def _fetch_chunks(response, q):
    try:
        for chunk in response:
            q.put(("chunk", chunk))
        q.put(("done", None))
    except Exception as e:
        q.put(("error", e))

def stream_response(messages, use_fallback=False):
    """
    Stream LLM response and yield complete sentences as they form.
    Matches the streaming logic from gemini_test.py exactly.
    Uses pre-cached config for zero overhead.
    """
    model_to_use = _FALLBACK_MODEL if use_fallback and _FALLBACK_MODEL else _MODEL
        
    print(f"\n--- Starting {model_to_use} Stream ---\n")

    try:
        response = completion(
            model=model_to_use,
            messages=messages,
            tools=TOOLS if TOOLS else None,
            stream=True,
            temperature=_TEMPERATURE,
            vertex_credentials=vertex_credentials_json if "vertex_ai" in model_to_use else None,
            vertex_location="global" if "vertex_ai" in model_to_use else None
        )
    except Exception as e:
        print(f"\n[LLM] Error starting {model_to_use}: {e}")
        if not use_fallback and _FALLBACK_MODEL:
            print(f"Falling back to {_FALLBACK_MODEL}...\n")
            yield from stream_response(messages, use_fallback=True)
            return
        else:
            yield ("done", "")
            return

    q = queue.Queue()
    t = threading.Thread(target=_fetch_chunks, args=(response, q), daemon=True)
    t.start()
    
    start_time = time.time()

    buffer = ""
    full_content = ""
    tool_calls = []

    while True:
        if not full_content and not use_fallback and _FALLBACK_MODEL:
            elapsed = time.time() - start_time
            remaining = _FALLBACK_TIMEOUT - elapsed
            if remaining <= 0:
                print(f"\n[LLM] {model_to_use} timed out waiting for content. Falling back...\n")
                yield from stream_response(messages, use_fallback=True)
                return
            
            try:
                msg_type, data = q.get(timeout=remaining)
            except queue.Empty:
                print(f"\n[LLM] {model_to_use} timed out waiting for content. Falling back...\n")
                yield from stream_response(messages, use_fallback=True)
                return
        else:
            msg_type, data = q.get()

        if msg_type == "done":
            break
        elif msg_type == "error":
            print(f"\n[LLM] Stream error: {data}")
            if not use_fallback and _FALLBACK_MODEL and not full_content:
                print(f"Falling back to {_FALLBACK_MODEL}...\n")
                yield from stream_response(messages, use_fallback=True)
                return
            else:
                break
                
        # msg_type == "chunk"
        chunk = data
        delta = chunk.choices[0].delta

        # 1. Stream the Model's "Thoughts" (Reasoning) - Exactly like gemini_test.py
        if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
            thought_chunk = delta.reasoning_content
            print(f"\033[90m[Thought]: {thought_chunk}\033[0m", end="", flush=True)

        # 2. Stream the Content
        if delta.content:
            content_chunk = delta.content
            full_content += content_chunk
            print(content_chunk, end="", flush=True)

            # --- KikiFast sentence-level yielding ---
            buffer += content_chunk
            sentences, buffer = _extract_sentences(buffer)
            for s in sentences:
                s = s.strip()
                if s:
                    yield ("sentence", s)

        # 3. Collect Tool Calls
        if delta.tool_calls:
            for tc in delta.tool_calls:
                index = tc.index
                if len(tool_calls) <= index:
                    tool_calls.append(tc)
                else:
                    tool_calls[index].function.arguments += tc.function.arguments

    # Flush remaining buffer
    if buffer.strip():
        yield ("sentence", buffer.strip())

    print("\n") # newline after stream

    # 4. Handle Parallel Tool Calls
    if tool_calls:
        print("--- Processing Tool Calls ---")
        
        messages.append({
            "role": "assistant",
            "content": full_content,
            "tool_calls": tool_calls
        })

        for tool_call in tool_calls:
            function_name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)
            
            print(f"Calling tool: {function_name} with {args}")
            result = execute_tool(function_name, args)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": function_name,
                "content": result
            })

        # Recursive call to get the final summary (streaming)
        yield from stream_response(messages, use_fallback=use_fallback)
        return

    yield ("done", full_content)


if __name__ == "__main__":
    cfg = _LLM_CFG
    messages = [
        {"role": "system", "content": cfg["system_prompt"]},
        {"role": "user", "content": "What's the weather like in Tokyo?"}
    ]

    for evt, data in stream_response(messages):
        if evt == "sentence":
            print(f"\n[Sentence] → {data}")
        elif evt == "done":
            print(f"\n[Done] Full: {data}")
