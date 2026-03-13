"""
Summary Manager
===============

Handles loading and saving conversation summaries to persistent storage.
Supports both single-file summaries and timestamped conversation files.

Ported from KIKI-SMART — standalone, no LiveKit dependencies.
"""

import asyncio
import concurrent.futures
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from tools_and_config.config_loader import get_full_config
from paths import PROJECT_ROOT


# ============================================================================
# Configuration Helpers
# ============================================================================

def get_summary_file_path() -> Path:
    config = get_full_config()
    return Path(config.get("agent", {}).get(
        "summary_file_path",
        str(PROJECT_ROOT / "conversation_summary.txt")
    ))


def get_conversations_folder_path() -> Path:
    config = get_full_config()
    folder = config.get("agent", {}).get("conversations_folder_path", "conversations")
    if not Path(folder).is_absolute():
        folder = PROJECT_ROOT / folder
    return Path(folder)


def get_past_conversations_count() -> int:
    config = get_full_config()
    return config.get("agent", {}).get("past_conversations_count", 5)


# ============================================================================
# Single-File Summary Functions
# ============================================================================

def load_saved_summary(file_path: Optional[Path] = None) -> Optional[str]:
    summary_file = file_path or get_summary_file_path()
    if summary_file.exists():
        try:
            content = summary_file.read_text().strip()
            if content:
                print(f"Loaded conversation summary from {summary_file}")
                return content
        except Exception as e:
            print(f"Error loading summary: {e}")
    return None


def save_summary(summary: str, file_path: Optional[Path] = None) -> bool:
    summary_file = file_path or get_summary_file_path()
    try:
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        summary_file.write_text(summary)
        print(f"Saved conversation summary to {summary_file}")
        return True
    except Exception as e:
        print(f"Error saving summary: {e}")
        return False


def delete_summary(file_path: Optional[Path] = None) -> bool:
    summary_file = file_path or get_summary_file_path()
    try:
        if summary_file.exists():
            summary_file.unlink()
            print(f"Deleted conversation summary: {summary_file}")
        return True
    except Exception as e:
        print(f"Error deleting summary: {e}")
        return False


# ============================================================================
# Timestamped Conversation Functions
# ============================================================================

def get_all_conversation_files() -> List[Path]:
    folder = get_conversations_folder_path()
    if not folder.exists():
        return []
    try:
        files = list(folder.glob("*.txt"))
        files.sort(key=lambda f: f.name, reverse=True)
        return files
    except Exception as e:
        print(f"[Summary] Error listing conversation files: {e}")
        return []


def save_summary_to_conversations_folder(summary: str) -> Optional[Path]:
    folder = get_conversations_folder_path()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now()
        filename = timestamp.strftime("%Y-%m-%d_%H-%M-%S.txt")
        file_path = folder / filename
        header = f"Conversation Summary\nDate: {timestamp.strftime('%Y-%m-%d')}\nTime: {timestamp.strftime('%H:%M:%S')}\n{'='*50}\n\n"
        content = header + summary
        file_path.write_text(content)
        print(f"[Summary] Saved to {file_path}")
        return file_path
    except Exception as e:
        print(f"[Summary] Error saving to conversations folder: {e}")
        return None


def load_latest_conversation() -> Optional[str]:
    files = get_all_conversation_files()
    if not files:
        print("[Summary] No previous conversation files found")
        return None
    latest_file = files[0]
    try:
        content = latest_file.read_text().strip()
        print(f"[Summary] Loaded latest conversation from {latest_file.name}")
        return content
    except Exception as e:
        print(f"[Summary] Error loading latest conversation: {e}")
        return None


async def generate_past_conversations_summary(n: Optional[int] = None) -> Optional[str]:
    """Generate a combined summary of the past N conversation files using LLM."""
    if n is None:
        n = get_past_conversations_count()

    files = get_all_conversation_files()
    past_files = files[1:n+1] if len(files) > 1 else []

    if not past_files:
        print("[Summary] Not enough past conversation files for summary")
        return None

    cache_file = get_conversations_folder_path() / "cached_past_summary.txt"
    try:
        if cache_file.exists():
            if cache_file.stat().st_mtime >= past_files[0].stat().st_mtime:
                print("[Summary] Loading past conversations summary from cache")
                return cache_file.read_text().strip()
    except Exception as e:
        print(f"[Summary] Error checking past conversations cache: {e}")

    try:
        past_summaries = []
        for f in past_files:
            try:
                content = f.read_text().strip()
                if "=" * 50 in content:
                    content = content.split("=" * 50, 1)[1].strip()
                past_summaries.append(f"[{f.stem}]:\n{content}")
            except Exception as e:
                print(f"[Summary] Error reading {f.name}: {e}")

        if not past_summaries:
            return None

        combined = "\n\n---\n\n".join(past_summaries)

        config = get_full_config()
        prompt_template = config.get("prompts", {}).get(
            "past_conversations_summary_prompt",
            "Summarize these past conversations briefly:\n{past_summaries}"
        )
        prompt = prompt_template.format(past_summaries=combined)

        from core.brain.generate_llm_resp import generate

        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            summary = await loop.run_in_executor(
                pool,
                lambda: generate(prompt, purpose="summary")
            )

        if summary:
            print(f"[Summary] Generated past conversations summary ({len(past_files)} files)")
            try:
                cache_file.write_text(summary)
            except Exception as e:
                print(f"[Summary] Error saving past conversations cache: {e}")
            return summary

        return None

    except Exception as e:
        print(f"[Summary] Error generating past conversations summary: {e}")
        return None
