"""
Tool definitions and handlers for KikiFast voice assistant.
All tools from KIKI-SMART ported as standalone functions (no LiveKit).

Each tool has:
  1. An async handler function
  2. An OpenAI function-calling schema in the TOOLS list
  3. A sync wrapper in _TOOL_HANDLERS for the LLM tool-calling loop
"""

import asyncio
import json
import os
import platform
import signal
import subprocess
import sys
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple, List

from paths import PROJECT_ROOT

from tools_and_config.config_loader import get_full_config


# ============================================================================
# Lazy imports (heavy modules loaded on first use)
# ============================================================================

_exa_client = None
_controller = None
_controller_lock = asyncio.Lock()


def _get_exa():
    global _exa_client
    if _exa_client is None:
        from exa_py import Exa
        exa_key = os.getenv("EXA_API_KEY")
        if not exa_key:
            raise ValueError("EXA_API_KEY not found in environment. Set it in your .env file.")
        _exa_client = Exa(exa_key)
    return _exa_client


async def _get_controller():
    """Get or create a shared KikiController instance."""
    global _controller
    async with _controller_lock:
        if _controller is None or not _controller._connected:
            from kiki_control_client import KikiController
            config = get_full_config()
            ctrl_config = config.get("controller", {})
            host = ctrl_config.get("host", "192.168.1.11")
            _controller = KikiController(host=host)
            connected = await _controller.connect()
            if not connected:
                raise RuntimeError(f"Failed to connect to KikiController at {host}")
            print(f"[Tools] Connected to KikiController at {host}")
    return _controller


# ============================================================================
# Tool Implementations (async)
# ============================================================================

async def search_web(query: str, search_range: str = "today") -> str:
    """Search the web for information using Exa."""
    try:
        exa = _get_exa()
        now = datetime.now()

        if search_range == "last 5 days":
            start_date = now - timedelta(days=5)
        elif search_range == "last month":
            start_date = now - timedelta(days=30)
        else:
            start_date = now - timedelta(hours=24)

        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: exa.search(
                query,
                num_results=3,
                type="auto",
                user_location="IN",
                contents={"highlights": True}
            )
        )

        listofresults = []
        if result and result.results:
            for i in result.results:
                if getattr(i, 'highlights', None):
                    listofresults.append(i.highlights)

        if not listofresults:
            return "No results found."
        print(str(listofresults))
        return str(listofresults)

    except Exception as e:
        print(f"[Search] Error: {e}")
        return f"Error performing web search: {str(e)}"


async def execute_shell_command(command: str) -> str:
    """Execute a shell command and return the output."""
    try:
        print(f"[System Tool] Executing: {command}")
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                command, shell=True, capture_output=True,
                text=True, timeout=10
            )
        )

        output = ""
        if result.stdout:
            output += f"Output:\n{result.stdout.strip()}\n"
        if result.stderr:
            output += f"Error/Stderr:\n{result.stderr.strip()}"
        if not output:
            output = "Command executed with no output."
        return output

    except subprocess.TimeoutExpired:
        return "Error: Command execution timed out (limit: 10s)."
    except Exception as e:
        return f"Error executing command: {str(e)}"


async def get_current_time() -> str:
    """Get the current time."""
    now = datetime.now()
    return now.strftime("%I:%M %p on %A, %B %d, %Y")


async def play_music(song: str) -> str:
    """Play a song from YouTube."""
    config = get_full_config()
    music_config = config.get("tools", {}).get("music", {})
    alternative_suffixes = music_config.get("alternative_search_suffixes", ["music", "audio", "official"])

    def _play_sync(search_query: str) -> Tuple[bool, str]:
        try:
            yt_kwargs = {}
            if platform.system() == "Windows":
                yt_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            yt_proc = subprocess.run(
                ["yt-dlp", "-f", "ba", f"ytsearch:{search_query}", "-g"],
                capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace", **yt_kwargs
            )
            url = yt_proc.stdout.strip().split('\n')[0]
            if not url or not url.startswith("http"):
                return False, f"Could not find an audio URL for {search_query}"

            cmd = ["mpv", url]
            popen_kwargs = dict(
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"
            )
            if platform.system() != "Windows":
                popen_kwargs["preexec_fn"] = os.setsid
            else:
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                
            proc = subprocess.Popen(cmd, **popen_kwargs)
            
            success = False
            start_wait = time.time()
            # Wait up to 7 seconds for audio to start
            while time.time() - start_wait < 7:
                line = proc.stdout.readline()
                if not line:
                    break
                if "A:" in line:
                    success = True
                    break
                    
            if success:
                return True, f"Now playing: {search_query}"
            else:
                try:
                    if platform.system() == "Windows":
                        proc.terminate()
                    else:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    pass
                return False, "Audio did not start playing within 7 seconds."
        except Exception as e:
            return False, str(e)

    async def try_play(search_query: str) -> Tuple[bool, str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _play_sync, search_query)

    success, message = await try_play(song)
    if success:
        print(f"Playing music: {song}")
        return f"Now playing {song}"

    print(f"First attempt failed: {message}. Trying alternatives...")
    for suffix in alternative_suffixes:
        success, message = await try_play(f"{song} {suffix}")
        if success:
            print(f"Playing music with alternative: {song} {suffix}")
            return f"Now playing {song}"

    return f"Sorry, I couldn't find or play {song}."


async def set_timer(duration: int) -> str:
    """Set a timer that alerts after the specified duration in seconds."""
    config = get_full_config()
    timer_config = config.get("tools", {}).get("timer", {})
    default_sound = str(PROJECT_ROOT / "sound_effects" / "soundeffects" / "timer.mp3")
    sound_path = timer_config.get("sound_effect_path", default_sound)
    # Resolve relative paths against PROJECT_ROOT
    if not os.path.isabs(sound_path):
        sound_path = str(PROJECT_ROOT / sound_path)

    def _play_after_delay():
        time.sleep(duration)
        try:
            subprocess.run(
                ["mpv", "--no-video", sound_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception as e:
            print(f"[Timer] Error playing sound: {e}")

    threading.Thread(target=_play_after_delay, daemon=True).start()
    print(f"Timer set for {duration} seconds")

    if duration >= 60:
        minutes = duration // 60
        seconds = duration % 60
        if seconds:
            return f"Timer set for {minutes} minutes and {seconds} seconds"
        return f"Timer set for {minutes} minutes"
    return f"Timer set for {duration} seconds"


async def update_knowledge(category: str, action: str, key: str,
                           value: str = "", attribute: str = "") -> str:
    """Store or retrieve information from long-term memory."""
    from core.brain.knowledge_base import get_knowledge_base, save_knowledge_base

    kb = get_knowledge_base()

    try:
        if category == "self":
            key = "Kiki"
            category = "people"

        if category == "people":
            if action in ("add", "update"):
                if attribute:
                    if attribute in ("routine", "interests", "hobbies"):
                        values = [v.strip() for v in value.split(",")]
                        for v in values:
                            kb.add_person_attribute(key, attribute, v, append=True)
                        result = f"Added to {key}'s {attribute}: {values}"
                    elif attribute == "notes":
                        kb.add_note_to_person(key, value)
                        result = f"Added note about {key}: {value}"
                    elif attribute == "character":
                        kb.set_person_character(key, value)
                        result = f"Set {key}'s character to: {value}"
                    elif attribute == "current_ongoing":
                        kb.set_current_ongoing(key, value)
                        result = f"Set {key}'s current situation: {value}"
                    elif attribute == "appearance":
                        kb.add_person_attribute(key, "appearance", value)
                        result = f"Set {key}'s appearance: {value}"
                    else:
                        kb.add_person_attribute(key, attribute, value)
                        result = f"Set {key}.{attribute} = {value}"
                else:
                    if "," in value and not value.startswith("relationship:"):
                        traits = [t.strip() for t in value.split(",")]
                        kb.add_person(key, traits=traits)
                        result = f"Added/updated {key} with traits: {traits}"
                    elif value.startswith("relationship:"):
                        kb.add_person(key, relationship=value.replace("relationship:", "").strip())
                        result = f"Set {key}'s relationship: {value}"
                    else:
                        kb.add_person(key, notes=value)
                        result = f"Added note about {key}: {value}"
            elif action == "get":
                if attribute:
                    attr_value = kb.get_person_attribute(key, attribute)
                    result = f"{key}'s {attribute}: {attr_value}" if attr_value else f"No {attribute} for {key}"
                else:
                    person = kb.get_person(key)
                    result = f"Info about {key}: {person}" if person else f"No info about {key}"
            elif action == "remove":
                result = f"Removed {key}" if kb.remove_person(key) else f"{key} not found"
            else:
                result = f"Unknown action: {action}"

        elif category == "environments":
            if action in ("add", "update"):
                if attribute == "description":
                    kb.add_environment(key, description=value)
                elif attribute:
                    kb.update_environment(key, attribute, value)
                else:
                    kb.add_environment(key, description=value)
                result = f"Environment '{key}': {value}"
            elif action == "get":
                env = kb.get_environment(key)
                result = f"Environment '{key}': {env}" if env else f"Unknown environment: {key}"
            elif action == "remove":
                result = f"Removed environment: {key}" if kb.remove_environment(key) else f"Not found: {key}"
            else:
                result = f"Unknown action: {action}"

        elif category == "learnings":
            if action in ("add", "update"):
                kb.add_learning(key, value)
                result = f"Learned [{key}]: {value}"
            elif action == "get":
                learnings = kb.get_learnings(key)
                result = f"Learnings about {key}: {learnings}" if learnings else f"No learnings about {key}"
            else:
                result = f"Unknown action: {action}"

        elif category == "experiences":
            if action == "add":
                parts = value.split("|", 1)
                outcome = parts[0].strip() if parts else "neutral"
                details = parts[1].strip() if len(parts) > 1 else None
                kb.add_experience(key, outcome=outcome, details=details)
                result = f"Logged experience: {key} ({outcome})"
            elif action == "get":
                experiences = kb.get_recent_experiences(10)
                result = f"Recent experiences: {experiences}"
            else:
                result = f"Unknown action: {action}"

        elif category == "facts":
            if action in ("add", "update"):
                kb.add_fact(key, value)
                result = f"Stored fact: {key} = {value}"
            elif action == "get":
                fact = kb.get_fact(key)
                result = f"{key}: {fact}" if fact is not None else f"Unknown fact: {key}"
            elif action == "remove":
                result = f"Removed fact: {key}" if kb.remove_fact(key) else f"Not found: {key}"
            else:
                result = f"Unknown action: {action}"

        elif category == "personality":
            if action == "add":
                kb.add_trait(key)
                result = f"Added personality trait: {key}"
            elif action == "update":
                kb.set_preference(key, value)
                result = f"Set preference: {key} = {value}"
            elif action == "get":
                personality = kb.get_personality()
                result = f"My personality: {personality}"
            else:
                result = f"Unknown action: {action}"

        elif action == "search":
            results = kb.search(key)
            if results:
                result = f"Search results for '{key}':\n" + "\n".join(
                    f"[{cat}] {', '.join(items)}" for cat, items in results.items()
                )
            else:
                result = f"No results found for '{key}'"
        else:
            result = f"Unknown category: {category}"

        if action in ("add", "update", "remove"):
            save_knowledge_base()

        print(f"[KnowledgeBase Tool] {result}")
        return result

    except Exception as e:
        return f"Error updating knowledge base: {e}"


async def remember_me(person_name: str) -> str:
    """Remember a person's face via face training."""
    try:
        print(f"[Robot Control] Starting face training for: {person_name}")
        controller = await _get_controller()
        response = await controller.train_person(person_name)

        if response.get("status") == "ok":
            print(f"[Robot Control] Training initiated for {person_name}")

            async def wait_for_training_complete():
                try:
                    async for event in controller.listen_events():
                        if event.get("event") == "training_complete":
                            if event.get("person") == person_name:
                                print(f"[Robot Control] Training complete for {person_name}")
                                break
                except Exception as e:
                    print(f"[Robot Control] Error waiting for training: {e}")

            asyncio.create_task(wait_for_training_complete())
            return f"I'm now remembering your face, {person_name}. Please stay still and look at me for about 10 seconds."
        else:
            error = response.get("error", "Unknown error")
            return f"Sorry, I couldn't start face training. Error: {error}"
    except Exception as e:
        return f"Sorry, error starting face training: {str(e)}"


async def track_person(person_name: str) -> str:
    """Set a specific person to track with the robot's neck."""
    try:
        controller = await _get_controller()
        success = await controller.set_target_person(person_name)
        if success:
            return f"I'm now tracking {person_name}. I'll follow them with my gaze."
        return f"Sorry, I couldn't set {person_name} as tracking target."
    except Exception as e:
        return f"Sorry, error setting tracking target: {str(e)}"


async def follow_me(duration: int) -> str:
    """Enable full body following mode."""
    try:
        controller = await _get_controller()
        success = await controller.set_full_body_movement(True)
        if success:
            return "I'm now following you! Say 'stop' to stop."
        return "Sorry, I couldn't enable follow mode."
    except Exception as e:
        return f"Sorry, error enabling follow mode: {str(e)}"


async def dance(song: str, steps: list) -> str:
    """Perform a choreographed dance routine with music."""
    try:
        import motor_control
    except ImportError:
        motor_control = None
        print("[Dance] Warning: motor_control not found")

    _dance_stop_event = threading.Event()

    def _dance_worker():
        nonlocal motor_control
        # Start music
        try:
            yt_kwargs = {}
            if platform.system() == "Windows":
                yt_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            yt_proc = subprocess.run(
                ["yt-dlp", "-f", "ba", f"ytsearch:{song}", "-g"],
                capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace", **yt_kwargs
            )
            url = yt_proc.stdout.strip().split('\n')[0]
            if not url or not url.startswith("http"):
                print(f"[Dance] Could not find music URL for {song}")
                return

            cmd = ["mpv", url]
            popen_kwargs = dict(
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"
            )
            if platform.system() != "Windows":
                popen_kwargs["preexec_fn"] = os.setsid
            else:
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            music_proc = subprocess.Popen(cmd, **popen_kwargs)
        except Exception as e:
            print(f"[Dance] Error starting music: {e}")
            return

        # Wait for music start
        music_started = False
        start_wait = time.time()
        while True:
            if _dance_stop_event.is_set() or time.time() - start_wait > 30:
                break
            line = music_proc.stdout.readline()
            if not line:
                break
            if "A:" in line:
                music_started = True
                break

        if not music_started:
            try:
                if platform.system() == "Windows":
                    music_proc.terminate()
                else:
                    os.killpg(os.getpgid(music_proc.pid), signal.SIGTERM)
            except:
                pass
            return

        try:
            if motor_control:
                motor_control.init_gpio()

            for step_data in steps:
                if _dance_stop_event.is_set():
                    break
                if isinstance(step_data, str):
                    import ast
                    step_data = ast.literal_eval(step_data)
                if not isinstance(step_data, dict):
                    continue

                interval = float(step_data.get('interval', 0))
                move = step_data.get('step', '').lower().replace(' ', '_')
                duration_s = float(step_data.get('duration', 0.5))
                duration_s = max(0.1, min(15, duration_s))
                speed = step_data.get('speed', 50)

                slept = 0
                while slept < interval:
                    if _dance_stop_event.is_set():
                        break
                    chunk = min(0.1, interval - slept)
                    time.sleep(chunk)
                    slept += chunk

                if _dance_stop_event.is_set():
                    break

                if motor_control:
                    move_func = getattr(motor_control, move, None)
                    if move_func:
                        try:
                            if speed is not None:
                                motor_control.update_speed(max(30, min(100, int(speed))))
                            move_func()
                            time.sleep(duration_s)
                            motor_control.stop()
                        except Exception as e:
                            print(f"[Dance] Error executing {move}: {e}")
                            motor_control.stop()
        finally:
            try:
                if platform.system() == "Windows":
                    music_proc.terminate()
                else:
                    os.killpg(os.getpgid(music_proc.pid), signal.SIGTERM)
            except:
                pass
            if platform.system() == "Windows":
                subprocess.run("taskkill /F /IM mpv.exe", shell=True, capture_output=True)
            else:
                subprocess.run("pkill mpv", shell=True, capture_output=True)
            if motor_control:
                motor_control.stop()
                motor_control.release_gpio()

    dance_thread = threading.Thread(target=_dance_worker, daemon=True)
    dance_thread.start()
    return f"Dance routine started with song '{song}'! Performing {len(steps)} choreographed steps."


# ============================================================================
# Worker Tools
# ============================================================================

async def schedule_worker(name: str, task_description: str, trigger_type: str,
                          trigger_value: str = "", conditions: str = "") -> str:
    """
    Schedule an autonomous worker task.
    
    trigger_type: "scheduled_time", "event", or "recurring"
    trigger_value: ISO datetime for scheduled_time, event name for event, seconds for recurring
    conditions: JSON string of conditions list, e.g. '[{"condition_type": "person_seen", "params": {"person": "Vaibhav", "within_minutes": 60}}]'
    """
    try:
        from core.workers.worker_manager import get_worker_manager
        manager = get_worker_manager()

        # Parse conditions if provided
        parsed_conditions = None
        if conditions and conditions.strip():
            try:
                parsed_conditions = json.loads(conditions)
            except json.JSONDecodeError:
                return f"Error: Invalid conditions JSON: {conditions}"

        worker = manager.create_worker(
            name=name,
            task_description=task_description,
            trigger_type=trigger_type,
            trigger_value=trigger_value,
            conditions=parsed_conditions,
        )
        return f"Worker scheduled: {worker}"
    except ValueError as e:
        return f"Error scheduling worker: {e}"
    except Exception as e:
        return f"Error scheduling worker: {e}"


async def cancel_worker(worker_id: str) -> str:
    """Cancel a pending worker by its ID or name."""
    try:
        from core.workers.worker_manager import get_worker_manager
        manager = get_worker_manager()
        success = manager.cancel_worker(worker_id)
        if success:
            return f"Worker '{worker_id}' cancelled successfully."
        return f"No active worker found with ID or name '{worker_id}'."
    except Exception as e:
        return f"Error cancelling worker: {e}"


async def list_workers() -> str:
    """List all active and pending workers."""
    try:
        from core.workers.worker_manager import get_worker_manager
        manager = get_worker_manager()
        summary = manager.get_status_summary()
        return summary
    except Exception as e:
        return f"Error listing workers: {e}"


async def execute_python_code(code: str) -> str:
    """Execute Python code in a subprocess and return the output."""
    try:
        print(f"[Tool] Executing Python code ({len(code)} chars)...")
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=30,
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
        return "Error: Code execution timed out after 30s."
    except Exception as e:
        return f"Error executing code: {e}"


# ============================================================================
# Tool Schemas (OpenAI function-calling format)
# ============================================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web for information using Exa. Use for latest news, facts, weather, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                    "search_range": {"type": "string", "enum": ["today", "last 5 days", "last month"], "description": "Time range"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_shell_command",
            "description": "Execute a shell command. Useful for checking system status like temperature, disk space, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current local time and date.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "play_music",
            "description": "Play a song or music from YouTube.",
            "parameters": {
                "type": "object",
                "properties": {
                    "song": {"type": "string", "description": "Song name, artist, or genre to search and play"}
                },
                "required": ["song"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": "Set a timer that will alert after the specified duration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "duration": {"type": "integer", "description": "Timer duration in seconds (e.g., 60 for 1 minute)"}
                },
                "required": ["duration"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_knowledge",
            "description": "Store or retrieve information from long-term memory. Categories: people, environments, learnings, experiences, facts, personality, self.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["people", "environments", "learnings", "experiences", "facts", "personality", "self"]},
                    "action": {"type": "string", "enum": ["add", "update", "remove", "get", "search"]},
                    "key": {"type": "string", "description": "Identifier - person name, place name, topic, etc."},
                    "value": {"type": "string", "description": "The value to store"},
                    "attribute": {"type": "string", "description": "For people: appearance, character, routine, interests, current_ongoing, notes"}
                },
                "required": ["category", "action", "key"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remember_me",
            "description": "Remember a person's face. Call when user asks to be remembered.",
            "parameters": {
                "type": "object",
                "properties": {
                    "person_name": {"type": "string", "description": "Name of the person to remember"}
                },
                "required": ["person_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "track_person",
            "description": "Set a specific person to track/follow with the robot's neck.",
            "parameters": {
                "type": "object",
                "properties": {
                    "person_name": {"type": "string", "description": "Name of the person to track"}
                },
                "required": ["person_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "follow_me",
            "description": "Enable full body following mode to follow the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "duration": {"type": "integer", "description": "How long to follow in seconds"}
                },
                "required": ["duration"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "dance",
            "description": "Perform a choreographed dance routine with music.",
            "parameters": {
                "type": "object",
                "properties": {
                    "song": {"type": "string", "description": "Song to play from YouTube"},
                    "steps": {
                        "type": "array",
                        "description": "List of dance steps with step, interval, duration, speed",
                        "items": {
                            "type": "object",
                            "properties": {
                                "step": {"type": "string"},
                                "interval": {"type": "number"},
                                "duration": {"type": "number"},
                                "speed": {"type": "integer"}
                            }
                        }
                    }
                },
                "required": ["song", "steps"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_worker",
            "description": "Schedule an autonomous background worker task. Workers are LLM-powered agents that run at specific times, on events (startup/shutdown/sleep/wake/after_response/face_detected), or on recurring intervals. Workers have full tool access and retry on failure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Human-readable name of the worker task"},
                    "task_description": {"type": "string", "description": "Detailed description of what the worker should do. Be specific about the task, tools to use, and desired outcome."},
                    "trigger_type": {"type": "string", "enum": ["scheduled_time", "event", "recurring"], "description": "When to trigger: scheduled_time (specific datetime), event (lifecycle event), or recurring (every N seconds)"},
                    "trigger_value": {"type": "string", "description": "For scheduled_time: ISO datetime (e.g. '2026-03-04T17:00:00'). For event: event name (startup/shutdown/sleep/wake/after_response/face_detected). For recurring: interval in seconds."},
                    "conditions": {"type": "string", "description": "Optional JSON array of conditions. Example: '[{\"condition_type\": \"person_seen\", \"params\": {\"person\": \"Vaibhav\", \"within_minutes\": 60}}]'"}
                },
                "required": ["name", "task_description", "trigger_type", "trigger_value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_worker",
            "description": "Cancel a pending or active worker by its ID or name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_id": {"type": "string", "description": "ID or name of the worker to cancel"}
                },
                "required": ["worker_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_workers",
            "description": "List all active and pending workers with their status.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_python_code",
            "description": "Execute Python code in a subprocess and return the output. Useful for calculations, data processing, file operations, or any task that benefits from code execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"}
                },
                "required": ["code"]
            }
        }
    },
]

# ============================================================================
# Tool Dispatch
# ============================================================================

# Map tool names to async handler functions
_ASYNC_TOOL_HANDLERS = {
    "search_web": search_web,
    "execute_shell_command": execute_shell_command,
    "get_current_time": get_current_time,
    "play_music": play_music,
    "set_timer": set_timer,
    "update_knowledge": update_knowledge,
    "remember_me": remember_me,
    "track_person": track_person,
    "follow_me": follow_me,
    "dance": dance,
    "schedule_worker": schedule_worker,
    "cancel_worker": cancel_worker,
    "list_workers": list_workers,
    "execute_python_code": execute_python_code,
}


def execute_tool(name: str, arguments: dict) -> str:
    """
    Execute a tool SYNCHRONOUSLY (for the LLM tool-calling loop in core/llm.py).
    Runs the async handler in a new event loop if needed.
    """
    handler = _ASYNC_TOOL_HANDLERS.get(name)
    if handler is None:
        return f"Error: Unknown tool '{name}'"

    try:
        # Try to use the running loop
        try:
            loop = asyncio.get_running_loop()
            # We're inside an event loop, create a task
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, handler(**arguments))
                return future.result(timeout=30)
        except RuntimeError:
            # No running loop, safe to use asyncio.run
            return asyncio.run(handler(**arguments))
    except Exception as e:
        return f"Error executing tool '{name}': {str(e)}"


async def execute_tool_async(name: str, arguments: dict) -> str:
    """Execute a tool ASYNCHRONOUSLY (for Big Brain tool execution)."""
    handler = _ASYNC_TOOL_HANDLERS.get(name)
    if handler is None:
        return f"Error: Unknown tool '{name}'"

    try:
        return await handler(**arguments)
    except Exception as e:
        return f"Error executing tool '{name}': {str(e)}"


def get_tool_descriptions() -> Dict[str, str]:
    """Get dict of tool_name -> description for Big Brain context."""
    return {
        tool["function"]["name"]: tool["function"]["description"]
        for tool in TOOLS
    }
