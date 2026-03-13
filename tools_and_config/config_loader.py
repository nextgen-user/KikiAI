"""
Configuration loader for KikiFast voice assistant.
Loads .env for API keys and config.json for all tunables.
"""

import os
import json
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

# Load config.json
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

with open(_CONFIG_PATH, "r") as f:
    CONFIG = json.load(f)


def get_full_config():
    """Returns the full configuration dict."""
    return CONFIG


def get_llm_config():
    """Returns LLM configuration dict."""
    return CONFIG["llm"]


def get_tts_config():
    """Returns TTS configuration dict."""
    return CONFIG["tts"]


def get_stt_config():
    """Returns STT configuration dict."""
    return CONFIG["stt"]


def get_sfx_config():
    """Returns sound effects configuration dict."""
    return CONFIG["sound_effects"]


def get_brain_config():
    """Returns Big Brain configuration dict."""
    return CONFIG.get("big_brain", {})


def get_tools_config():
    """Returns tools configuration dict."""
    return CONFIG.get("tools", {})


def get_controller_config():
    """Returns controller configuration dict."""
    return CONFIG.get("controller", {})


def get_prompts_config():
    """Returns prompts configuration dict."""
    return CONFIG.get("prompts", {})


def get_face_events_config():
    """Returns face events configuration dict."""
    return CONFIG.get("face_events", {})


def get_agent_config():
    """Returns agent configuration dict."""
    return CONFIG.get("agent", {})
