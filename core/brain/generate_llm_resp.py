"""
Multi-Provider LLM Router for Brain Operations
================================================

Used by Big Brain (reasoning), vision processing, and summarization.
This is the SLOW, HIGH-QUALITY path — completely separate from the fast
core/llm.py streaming pipeline.

Supports: Gemini (cloud) + Groq (cloud) + local models (LM Studio)
Ported from KIKI-SMART — standalone, no LiveKit dependencies.
"""

import os
import json
import base64
from google import genai
from google.genai import types
from openai import OpenAI
from groq import Groq

from tools_and_config.config_loader import get_full_config

# Load API keys from environment
KEY_LIST = json.loads(os.getenv("GEMINI_KEY_LIST", "[]"))
GROQ_KEY_LIST = json.loads(os.getenv("GROQ_API_KEY_LIST", "[]"))
GROQ_MODEL = "openai/gpt-oss-120b"

grounding_tool = types.Tool(
    google_search=types.GoogleSearch()
)


def _get_local_llm_config():
    """Load local LLM config from config.json."""
    config = get_full_config()
    return config.get("use_local_llm", {})


def _get_local_client(cfg):
    """Create an OpenAI-compatible client pointing at the local server."""
    return OpenAI(
        base_url=cfg.get("local_api_base", "http://100.81.223.97:1234/v1"),
        api_key=cfg.get("local_api_key", "lm-studio"),
    )


def _call_local_model(content, b64_image=None, model_name="nanbeige4.1-3b", cfg=None):
    """Call a local LLM via OpenAI-compatible API."""
    if cfg is None:
        cfg = _get_local_llm_config()

    client = _get_local_client(cfg)

    try:
        if b64_image and model_name == cfg.get("vision_model", "zwz-4b"):
            message_content = [
                {"type": "input_text", "text": content},
                {
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{b64_image}",
                },
            ]
        else:
            message_content = content

        response = client.responses.create(
            model=model_name,
            input=[{
                "role": "user",
                "content": message_content,
            }],
        )

        result = response.output_text
        print(f"[LocalLLM] {model_name} responded ({len(result)} chars)")
        return result

    except Exception as e:
        print(f"[LocalLLM] {model_name} failed: {e}")
        return None


def _call_gemini(content, b64_image=None, thinking_level="MEDIUM", websearch=False, model="gemini-3-flash-preview"):
    """Try all Gemini API keys for a specific model."""
    use_thinking = (model == "gemini-3-flash-preview")

    for API_KEY in KEY_LIST:
        try:
            client = genai.Client(api_key=API_KEY)

            config_kwargs = {}
            if use_thinking:
                config_kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_level=thinking_level.upper(),
                )
            config_kwargs["tools"] = [grounding_tool] if websearch else []
            # config_kwargs["temperature"]=1.05
            # config_kwargs["frequency_penalty"]=0.5
            

            generate_content_config = types.GenerateContentConfig(**config_kwargs)

            if b64_image is None:
                contents = [
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=content)],
                    ),
                ]
            else:
                contents = [
                    types.Content(
                        parts=[
                            types.Part(text=content),
                            types.Part(
                                inline_data=types.Blob(
                                    mime_type="image/jpeg",
                                    data=b64_image,
                                ),
                                media_resolution={"level": "media_resolution_high"}
                            )
                        ]
                    )
                ]

            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config=generate_content_config,
            )
            return resp.text

        except Exception:
            print(f"[Gemini] Key failed for {model}, trying next key...")

    return None


def _call_groq(content, thinking_level="MEDIUM"):
    """Try all Groq API keys with openai/gpt-oss-120b (text-only)."""
    effort_map = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high"}
    reasoning_effort = effort_map.get(thinking_level.upper(), "medium")

    for api_key in GROQ_KEY_LIST:
        try:
            client = Groq(api_key=api_key)
            chat_completion = client.chat.completions.create(
                messages=[{"role": "user", "content": content}],
                reasoning_effort=reasoning_effort,
                model=GROQ_MODEL,
            )
            result = chat_completion.choices[0].message.content
            print(f"[Groq] {GROQ_MODEL} responded ({len(result)} chars)")
            return result

        except Exception as e:
            print(f"[Groq] Key failed: {e}")

    return None


def generate(content, b64_image=None, thinking_level="MEDIUM", websearch=False, purpose="general"):
    """
    Generate a response using local LLMs and/or cloud, with smart routing.

    Args:
        content: The prompt text
        b64_image: Optional base64 encoded image
        thinking_level: "LOW", "MEDIUM", or "HIGH"
        websearch: Whether to enable web search grounding
        purpose: "general", "vision", "summary", or "reasoning"
    """
    cfg = _get_local_llm_config()

    use_vision_local = cfg.get("vision", False)
    use_summary_local = cfg.get("summary", False)
    use_reasoning_local = cfg.get("reasoning", False)

    vision_model = cfg.get("vision_model", "zwz-4b")
    text_model = cfg.get("text_model", "nanbeige4.1-3b")

    has_image = b64_image is not None
    local_primary = False
    local_model = text_model
    local_image = None

    if use_vision_local and has_image:
        local_primary = True
        local_model = vision_model
        local_image = b64_image
        print(f"[LLM Router] Local PRIMARY: {vision_model} (vision + image)")
    elif use_summary_local and purpose == "summary":
        local_primary = True
        local_model = text_model
        local_image = None
        print(f"[LLM Router] Local PRIMARY: {text_model} (summary)")
    elif use_reasoning_local and purpose == "reasoning":
        local_primary = True
        local_model = text_model
        local_image = None
        print(f"[LLM Router] Local PRIMARY: {text_model} (reasoning/big-brain)")

    if local_primary:
        result = _call_local_model(content, b64_image=local_image, model_name=local_model, cfg=cfg)
        if result:
            return result
        print(f"[LLM Router] Local {local_model} failed, falling back to Gemini cloud...")

    # Try gemini-3-flash-preview
    result = _call_gemini(content, b64_image, thinking_level, websearch, model="gemini-3-flash-preview")
    if result:
        return result
    print("[LLM Router] All gemini-3-flash keys exhausted!")

    # Try gemini-2.5-flash
    result = _call_gemini(content, b64_image, thinking_level, websearch, model="gemini-2.5-flash")
    if result:
        print("[LLM Router] Using gemini-2.5-flash")
        return result
    print("[LLM Router] All gemini-2.5-flash keys exhausted!")

    # Try Groq (text-only)
    if not has_image:
        result = _call_groq(content, thinking_level)
        if result:
            return result
        print("[LLM Router] All Groq keys exhausted!")
    else:
        print("[LLM Router] Skipping Groq (does not support images)")

    # LAST RESORT — local models even if flags are off
    if not local_primary:
        print("[LLM Router] ALL cloud providers failed! Using local models as LAST RESORT...")
        if has_image:
            result = _call_local_model(content, b64_image=b64_image, model_name=vision_model, cfg=cfg)
            if result:
                return result
        result = _call_local_model(content, b64_image=None, model_name=text_model, cfg=cfg)
        if result:
            return result
        print("[LLM Router] CRITICAL: All providers (cloud + local) failed!")

    return None
