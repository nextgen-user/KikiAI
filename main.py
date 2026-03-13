"""
KikiFast Voice Assistant — Main Orchestrator

Flow:
  1. Hotword thread listens in background. STT is started but muted.
  2. On 'heyy', STT unmutes immediately for low latency.
  3. STT listens for user speech.
  4. On endpoint → mute mic, start thinking sounds.
  5. Stream LLM response, extract movement tags, queue sentences to TTS.
  6. TTSStreamer pre-fetches audio in background.
  7. When first queue ready → stop thinking sounds, play.
  8. Sentences play back-to-back with no gaps (pre-fetched).
  9. After TTS finishes → trigger Big Brain analysis (background async task).
  10. Unmuted STT waits up to 10s for another query.
  11. If 10s passes with no speech, STT is muted until next 'heyy'.

Additional background tasks:
  - Face event listener via KikiController (greeting injection)
  - Big Brain async analysis (suggestion injection)
  - Knowledge base context injection

All modules are imported eagerly for minimum latency.
"""

import asyncio
import platform
import signal
import sys
import time
import re
import threading
import subprocess
import os
import concurrent.futures
import datetime

from hotwords.hotword_recog import HotwordRecognizer
from paths import PROJECT_ROOT, project_path

# --- Eager imports: triggers pre-initialization in each module ---
print("[Main] Initializing modules...")
t0 = time.time()

from tools_and_config.config_loader import get_llm_config, get_full_config, get_stt_config
from core.stt import STTEngine
from core.llm import stream_response
from core.tts import TTSStreamer
from sound_effects.sound_effects import ThinkingSoundPlayer

# Brain imports (lazy-ish: loaded now but Big Brain runs async later)
from core.brain.big_brain import analyze_conversation, get_suggestions_for_prompt, get_big_brain_config
from core.brain.knowledge_base import get_knowledge_summary, get_knowledge_base, save_knowledge_base
from core.brain.summary_manager import (
    save_summary, load_latest_conversation,
    save_summary_to_conversations_folder, generate_past_conversations_summary
)
from core.brain.generate_llm_resp import generate as generate_llm
from core.vision.vision_handler import VisionHandler
from robot.face_handler import face_event_listener
from core.brain import token_counter
from robot.movement import extract_movement_tags, strip_movement_tags, execute_movements
from tools_and_config.tools import get_tool_descriptions
from core.workers.worker_manager import get_worker_manager
from core.workers.worker_brain import get_face_history, get_vision_history
import random

print(f"[Main] All modules initialized in {time.time() - t0:.2f}s\n")

# Global reference to stop active TTS gracefully
active_tts_streamer = None







def _kill_mpv():
    """Kill all running mpv processes (cross-platform)."""
    try:
        if platform.system() == "Windows":
            subprocess.Popen(["taskkill", "/F", "/IM", "mpv.exe"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["pkill", "-9", "mpv"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[Main] Error killing mpv: {e}")


# ============================================================================
# STT Stream Bridge (thread → asyncio.Queue)
# ============================================================================

def stt_stream_worker(stt: STTEngine, event_queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
    """Run STT stream in a thread, push events to asyncio queue."""
    try:
        for event, text in stt.stream():
            loop.call_soon_threadsafe(event_queue.put_nowait, (event, text))
    except Exception as e:
        loop.call_soon_threadsafe(event_queue.put_nowait, ("error", str(e)))


# ============================================================================
# Main Async Loop
# ============================================================================

async def main():
    global active_tts_streamer
    global livestream_process  # Bring into scope

    print("[Main] Starting livestream.py in the background...")
    # Start livestream.py non-blockingly
    livestream_process = subprocess.Popen([sys.executable, "livestream.py"])
    cfg = get_llm_config()
    full_config = get_full_config()
    system_prompt = cfg.get("system_prompt", "You are Kiki.")
    agent_config = full_config.get("agent", {})
    prompts_config = full_config.get("prompts", {})

    # --- STT and Async state ---
    loop = asyncio.get_running_loop()
    stt_queue = asyncio.Queue()

    last_time_injected = time.time()
    summarizing = False
    turn_counter = 0
    current_system_context = ""

    # --- Peeping Config ---
    peeping_cfg = full_config.get("peeping", {})
    peeping_interval = peeping_cfg.get("interval_seconds", 0)
    peeping_listen_duration = peeping_cfg.get("listen_duration_seconds", 10)
    peeping_active = False
    peep_sentences = []

    # --- Vision Injection Config ---
    vision_injection_cfg = full_config.get("vision_injection", {})
    vision_injection_enabled = vision_injection_cfg.get("enabled", False)
    main_vision_cfg = vision_injection_cfg.get("main_llm", {})
    main_vision_enabled = vision_injection_enabled and main_vision_cfg.get("enabled", False)
    main_vision_every_n = main_vision_cfg.get("every_n_turns", 3)

    # --- Load Contexts ---
    print("[Main] Loading persistent memory contexts... this may take a moment.")
    
    # 1. Knowledge Base
    kb_summary = get_knowledge_summary(max_lines=full_config.get("knowledge_base", {}).get("max_context_lines", 50))
    
    # 2. Past Conversation Summaries (from conversations folder, excluding the latest)
    past_conversations_count = agent_config.get("past_conversations_count", 5)
    past_summary = None
    if past_conversations_count > 0:
        print(f"[Main] Generating past conversations summary (N={past_conversations_count})")
        past_summary = await generate_past_conversations_summary(past_conversations_count)

    # 3. Latest Conversation (very last session)
    past_conversation = load_latest_conversation()

    # Build context components
    additional_context = ""
    if kb_summary:
        kb_context_prompt = prompts_config.get("knowledge_context", "Things you remember about people and the world (your long-term memory):\n{knowledge_summary}")
        additional_context += f"\n\n## YOUR MEMORY (Knowledge Base)\n{kb_context_prompt.format(knowledge_summary=kb_summary)}"
    if past_summary:
        prev_sum_prompt = prompts_config.get("previous_summary_context", "Your memories from past conversations:\n{summary}")
        additional_context += f"\n\n## PAST CONVERSATIONS SUMMARY\n{prev_sum_prompt.format(summary=past_summary)}"
    if past_conversation:
        additional_context += f"\n\n## MOST RECENT CONVERSATION\n{past_conversation}"

    current_system_context = additional_context.strip()

    # Conversation history
    message_history = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": system_prompt,
                    # "cache_control": {"type": "ephemeral"}
                }
            ]
        }
    ]
    if current_system_context:
        message_history.append({"role": "system", "content": current_system_context})

    # Workers context will be injected dynamically at each response cycle
    # (see step 0 in the main loop)

    # Initialize modules
    stt = STTEngine()
    sfx = ThinkingSoundPlayer()
    stt.mute()

    # Graceful shutdown
    def shutdown(sig, frame):
        global livestream_process
        print("\n[Main] Shutting down...")
        if livestream_process and livestream_process.poll() is None:
                    print("[Main] Terminating livestream.py...")
                    livestream_process.terminate()
                    livestream_process.wait() # Wait for it to exit completely
        sfx.stop()
        stt.stop()
        # Save conversation on exit
        try:
            convo_text = "\n".join([
                f"{m['role'].upper()}: {m['content'] if isinstance(m['content'], str) else '[image]'}"
                for m in message_history[1:] if m.get('content') and m['role'] != 'system'
            ])
            if convo_text.strip():
                print("[Main] Generating final conversation summary...")
                sum_prompt = prompts_config.get("summarization_prompt", "Summarize this: {conversation}")
                prompt = sum_prompt.format(conversation=convo_text)
                from core.brain.generate_llm_resp import generate
                summary = generate(prompt, purpose="summary")
                if summary:
                    print(f"[Summarization] Generated summary:\n{summary[:200]}...")
                    save_summary_to_conversations_folder(summary)
                    save_summary(summary)
                    print("[Main] Conversation summarized and saved.")
                else:
                    save_summary_to_conversations_folder(convo_text)
                    print("[Main] Summarization failed, raw conversation saved.")
        except Exception as e:
            print(f"[Main] Error saving conversation: {e}")
        os._exit(0)

    signal.signal(signal.SIGINT, shutdown)

    print("=" * 50)
    print("  KikiFast Voice Assistant")
    print("  + Big Brain | + Tools | + Face Events | + Workers")
    print("=" * 50)
    print(f"  LLM:   {cfg['model']}")
    print(f"  Brain: {'ENABLED' if get_big_brain_config().get('enabled', True) else 'DISABLED'}")
    print(f"  KB:    {'Loaded' if kb_summary else 'Empty'}")
    print(f"  Press Ctrl+C to quit")
    print("=" * 50)
    print()

    # Timer logic for returning to hotword mode
    mute_timer = None
    mute_timer_lock = threading.Lock()

    def mute_stt():
        print("\n[Main] ⏱️ 10 seconds empty. Muting STT (Back to hotword mode).")
        stt.mute()

    def reset_mute_timer():
        nonlocal mute_timer
        with mute_timer_lock:
            if mute_timer is not None:
                mute_timer.cancel()
            mute_timer = threading.Timer(10.0, mute_stt)
            mute_timer.daemon = True
            mute_timer.start()

    def cancel_mute_timer():
        nonlocal mute_timer
        with mute_timer_lock:
            if mute_timer is not None:
                mute_timer.cancel()
                mute_timer = None

    # Sleep Mode State
    sleep_state = {
        "last_activity_time": time.time(),
        "is_sleeping": False
    }

    def check_sleep_mode():
        if not sleep_state["is_sleeping"]:
            elapsed = time.time() - sleep_state["last_activity_time"]
            SLEEP_TIMEOUT_S = 30 * 60
            if elapsed > SLEEP_TIMEOUT_S:
                sleep_state["is_sleeping"] = True
                print(f"[Sleep] Entering sleep mode after {elapsed/60:.1f} minutes of inactivity")
                # Fire sleep workers
                asyncio.run_coroutine_threadsafe(
                    get_worker_manager(loop).fire_event("sleep"), loop
                )
        return sleep_state["is_sleeping"]

    # Hotword background thread
    def hotword_thread_func():
        hotword_dir = project_path("hotwords")
        # Auto-detect .ppn files for the current platform
        _platform_tag = {"Windows": "windows", "Linux": "linux", "Darwin": "mac"}.get(platform.system(), "linux")
        keywords = [str(f) for f in hotword_dir.glob(f"*_{_platform_tag}_*.ppn")]
        if not keywords:
            # Fallback: try any .ppn file
            keywords = [str(f) for f in hotword_dir.glob("*.ppn")]
        if not keywords:
            print("[Hotword] No .ppn keyword files found in hotwords/ directory. Hotword detection disabled.")
            return
        print(f"[Hotword] Using keyword files: {[os.path.basename(k) for k in keywords]}")

        # Use configured device_index or None for auto-detect
        hw_device_index = full_config.get("agent", {}).get("audio_device_index", None)

        try:
            recognizer = HotwordRecognizer(keyword_paths=keywords, device_index=hw_device_index)
            print(str(PROJECT_ROOT / "sound_effects/soundeffects/tts.wav"))
            subprocess.run(["mpv", "--no-video", str(PROJECT_ROOT / "sound_effects/soundeffects/tts.wav")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for hotword in recognizer.listen():
                if "hey-kiki" in hotword:
                    sleep_state["last_activity_time"] = time.time()
                    if sleep_state["is_sleeping"]:
                        sleep_state["is_sleeping"] = False
                        print("[Sleep] Waking up from sleep mode due to hotword")
                        # Fire wake workers
                        asyncio.run_coroutine_threadsafe(
                            get_worker_manager(loop).fire_event("wake"), loop
                        )
                    if stt.is_muted:
                        print("\n[Hotword] 'Heyy' detected. Playing wake audio...")
                        def play_wake_audio_and_unmute():
                            try:
                                cfg_sfx = full_config.get("sound_effects", {})
                                wake_audios = cfg_sfx.get("wake_word_audio_effects", [])
                                if wake_audios:
                                    audio_file = random.choice(wake_audios)
                                    audio_path = str(PROJECT_ROOT / audio_file)
                                    if os.path.exists(audio_path):
                                        subprocess.run(["mpv", "--no-video", audio_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            except Exception as e:
                                print(f"[Hotword] Error playing wake audio: {e}")
                            
                            # time.sleep(0.5)
                            print("[Hotword] Unmuting STT now! 🔊")
                            stt.unmute()
                            reset_mute_timer()
                            
                        threading.Thread(target=play_wake_audio_and_unmute, daemon=True).start()
                    else:
                        reset_mute_timer()
                elif "stop-music" in hotword:
                    print(f"\n[Hotword] '{hotword}' detected. Killing mpv...")
                    _kill_mpv()
                    if active_tts_streamer:
                        try:
                            active_tts_streamer._sentence_queue.put(None)
                            while not active_tts_streamer._sentence_queue.empty():
                                active_tts_streamer._sentence_queue.get()
                        except:
                            pass
        except Exception as e:
            print(f"[Hotword] Error: {e}")

    hw_thread = threading.Thread(target=hotword_thread_func, daemon=True)
    hw_thread.start()

    # --- Vision and Autonomy State ---
    vision_handler = VisionHandler(full_config, loop, stt_queue, check_sleep_mode)

    # --- Workers System ---
    worker_manager = get_worker_manager(loop, message_history=message_history)
    worker_manager.start_scheduler()
    face_history = get_face_history()
    vision_history = get_vision_history()

    # --- Fire startup workers ---
    await worker_manager.fire_event("startup")

    # --- Start face event listener (background async task) ---
    face_task = asyncio.create_task(face_event_listener(message_history, stt, sleep_state, loop, stt_queue, face_history))

    # --- Vision Logic ---
    vision_task_ref = None

    # --- Periodic Question Background Loop ---
    async def periodic_question_loop():
        """Independent timer that triggers autonomous questions at configured intervals."""
        nonlocal vision_task_ref
        while True:
            await asyncio.sleep(30)  # Check every 30 seconds
            if check_sleep_mode():
                continue
            now = time.time()
            elapsed = now - vision_handler.last_question_time
            if elapsed >= vision_handler.next_question_interval:
                print(f"[Periodic] Question interval reached ({elapsed:.0f}s >= {vision_handler.next_question_interval}s). Triggering vision update...")
                if not vision_task_ref or vision_task_ref.done():
                    vision_task_ref = asyncio.create_task(vision_handler.run_vision_update())

    periodic_q_task = asyncio.create_task(periodic_question_loop())

    # --- Peeping Background Loop ---
    async def peeping_loop():
        """Periodically unmute STT to passively listen for ambient speech."""
        nonlocal peeping_active
        if peeping_interval <= 0:
            print("[Peeping] Disabled (interval_seconds=0)")
            return
        print(f"[Peeping] Enabled: every {peeping_interval}s, listen for {peeping_listen_duration}s")
        while True:
            await asyncio.sleep(peeping_interval)
            # Skip if sleeping
            if check_sleep_mode():
                continue
            # Skip if STT is already unmuted (user is actively talking)
            if not stt.is_muted:
                print("[Peeping] STT already unmuted (active conversation), skipping peep")
                continue
            print(f"[Peeping] Starting peep (listening for {peeping_listen_duration}s)...")
            peep_sentences.clear()
            peeping_active = True
            stt.unmute()
            await asyncio.sleep(peeping_listen_duration)
            stt.mute()
            peeping_active = False
            # Collect results
            heard = list(peep_sentences)
            peep_sentences.clear()
            if heard:
                heard_text = " ".join(heard)
                print(f"[Peeping] Collected: {heard_text}")
                message_history.append({
                    "role": "system",
                    "content": f"[Peeping — passively listening to surroundings to see what is going on right now]: {heard_text}"
                })
                print(f"[Peeping] Injected into history")
            else:
                print("[Peeping] Nothing heard")

    peeping_task = asyncio.create_task(peeping_loop())

    stt_thread = threading.Thread(
        target=stt_stream_worker,
        args=(stt, stt_queue, loop),
        daemon=True
    )
    stt_thread.start()

    # Track Big Brain analysis task
    brain_task = None
    collected_sentences = []
    available_tools = get_tool_descriptions()

    try:
        while True:
            try:
                event, text = await asyncio.wait_for(stt_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            if event == "error":
                print(f"[STT] Error: {text}")
                break

            if event == "final":
                # During peeping, route to peep buffer instead of conversation
                if peeping_active:
                    print(f"[Peeping] Heard: {text}")
                    peep_sentences.append(text)
                    continue

                print(f"[User] {text}")
                collected_sentences.append(text)
                
                sleep_state["last_activity_time"] = time.time()
                if sleep_state["is_sleeping"]:
                    sleep_state["is_sleeping"] = False
                    print("[Sleep] Waking up from sleep mode due to user speech")
                    
                if not stt.is_muted:
                    reset_mute_timer()

            elif event in ("endpoint", "autonomous_vision", "face_wake"):
                # During peeping, ignore endpoints entirely (no AI response)
                if peeping_active and event == "endpoint":
                    continue
                if event == "endpoint" and not collected_sentences:
                    continue

                if event == "face_wake":
                    # Known face woke us up. Trigger vision chain.
                    if not vision_task_ref or vision_task_ref.done():
                        vision_task_ref = asyncio.create_task(vision_handler.run_vision_update(force_trigger=True))
                    continue

                cancel_mute_timer()

                stt_cfg = get_stt_config()
                if not stt_cfg.get("aec_enabled", False):
                    stt.mute()
                else:
                    print("[Main] 🔊 Muting skipped while speaking (AEC enabled)")

                sfx.start()

                # 3. Context Injection (Time)
                now = time.time()
                time_injection_interval = agent_config.get("time_injection_threshold_minutes", 5) * 60
                if now - last_time_injected > time_injection_interval:
                    current_time_str = datetime.datetime.now().strftime("%I:%M %p")
                    time_prompt = prompts_config.get("current_time_context", "Right now it's {current_time}. Use this naturally.")
                    message_history.append({
                        "role": "system", 
                        "content": time_prompt.format(current_time=current_time_str)
                    })
                    last_time_injected = now
                    print(f"[Context] Injected current time: {current_time_str}")

                    # Also inject active workers context into system prompt
                    workers_context = worker_manager.get_workers_context_summary()
                    if workers_context or current_system_context:
                        new_context = current_system_context
                        if workers_context:
                            new_context += "\n\n" + workers_context
                        
                        if len(message_history) > 1 and message_history[1]["role"] == "system" and isinstance(message_history[1].get("content"), str):
                            message_history[1]["content"] = new_context
                        else:
                            message_history.insert(1, {"role": "system", "content": new_context})

                if event == "autonomous_vision":
                    user_utterance = ""
                    collected_sentences = []
                    
                    print(f"\n{'─' * 40}")
                    print(f"[Autonomous Vision Query Triggered]")
                    print(f"{'─' * 40}")
                    
                    question_instruction = prompts_config.get("periodic_question_instruction", "[FRIEND MODE] Make an observational comment or ask a genuine question about them.")
                    message_history.append({
                        "role": "system",
                        "content": f"{text}\n\n{question_instruction}"
                    })
                    print(f"[Context] Injected autonomous vision instruction")
                    bb_injection = None
                else:
                    # --- Build full user utterance ---
                    user_utterance = " ".join(collected_sentences)
                    collected_sentences = []

                    print(f"\n{'─' * 40}")
                    print(f"[User Query] {user_utterance}")
                    print(f"{'─' * 40}")
                    
                    if vision_handler.pending_vision_context:
                        message_history.append({
                            "role": "user",
                            "content": f"[SYSTEM] [VISION] {vision_handler.pending_vision_context}"  
                        })
                        print(f"[Context] Injected pending vision context")
                        vision_handler.pending_vision_context = None

                    now = time.time()
                    elapsed = now - vision_handler.last_question_time
                    if elapsed > vision_handler.next_question_interval:
                        question_prompt = prompts_config.get("periodic_question_instruction", "[FRIEND MODE] Make an observational comment or ask a genuine question about them.")
                        message_history.append({
                            "role": "system",
                            "content": question_prompt
                        })
                        vision_handler.last_question_time = now
                        vision_handler.next_question_interval = random.randint(
                            min(vision_handler.question_min_interval, vision_handler.question_max_interval), 
                            max(vision_handler.question_min_interval, vision_handler.question_max_interval)
                        )
                        print(f"[Context] Injected periodic question instruction")

                    # 4. Get Big Brain suggestions for this turn
                    bb_injection = await get_suggestions_for_prompt()

                    # 5. Add user message to history
                    message_history.append({"role": "user", "content": user_utterance})

                # --- Vision Injection (every N turns) ---
                turn_counter += 1
                if main_vision_enabled and turn_counter % main_vision_every_n == 0:
                    try:
                        from core.vision.camera import capture_photo_b64
                        vi_b64 = await loop.run_in_executor(None, capture_photo_b64)
                        if vi_b64:
                            message_history.append({
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "[Kiki's current view from camera — use this visual context naturally]"},
                                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{vi_b64}"}}
                                ]
                            })
                            print(f"[VisionInjection] Injected camera image into main LLM history (turn {turn_counter})")
                        else:
                            print(f"[VisionInjection] Camera capture failed, skipping (turn {turn_counter})")
                    except Exception as e:
                        print(f"[VisionInjection] Error injecting image: {e}")

                # Optionally inject Big Brain suggestions as a system hint
                if bb_injection:
                    message_history.append({
                        "role": "system",
                        "content": bb_injection
                    })
                    print(f"[BigBrain] Injected suggestions into prompt")

                # 6. Create TTS streamer
                tts_streamer = TTSStreamer()
                active_tts_streamer = tts_streamer
                tts_streamer.start()

                def stop_sfx_on_first_play():
                    tts_streamer.first_play_event.wait()
                    sfx.stop()

                sfx_stopper = threading.Thread(target=stop_sfx_on_first_play, daemon=True)
                sfx_stopper.start()

                # 6. Stream LLM response in a thread (it's sync/blocking)
                full_response = ""
                pending_movements = []

                def llm_and_tts():
                    nonlocal full_response, pending_movements
                    first_sentence = True
                    try:
                        for evt, data in stream_response(message_history):
                            if getattr(tts_streamer, 'interrupted', False):
                                print("[Main] LLM streaming interrupted!")
                                break
                            if evt == "sentence":
                                if first_sentence:
                                    first_sentence = False
                                    print(f"\n[TTS] Queueing sentences...")

                                # Extract movement tags before TTS
                                movements = extract_movement_tags(data)
                                if movements:
                                    pending_movements.extend(movements)
                                    print(f"[Movement] Found tags: {movements}")

                                # Strip movement tags for TTS
                                clean_text = strip_movement_tags(data)
                                if clean_text:
                                    tts_streamer.add_sentence(clean_text)
                                full_response += data + " "

                            elif evt == "done":
                                if first_sentence:
                                    sfx.stop()
                                full_response = data
                    except Exception as e:
                        print(f"[Main] LLM/TTS error: {e}")
                        sfx.stop()

                async def generate_and_play():
                    # Run LLM streaming in thread
                    await loop.run_in_executor(None, llm_and_tts)
                    # Signal no more sentences and wait for playback
                    await loop.run_in_executor(None, tts_streamer.finish)

                gen_task = asyncio.create_task(generate_and_play())
                
                # Wait for generation/playback while polling stt_queue for interruptions
                while not gen_task.done():
                    try:
                        inner_event, inner_text = await asyncio.wait_for(stt_queue.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        continue
                    
                    if inner_event == "error":
                        print(f"[STT] Error: {inner_text}")
                        break
                    
                    if inner_event == "final":
                        if peeping_active:
                            peep_sentences.append(inner_text)
                            continue
                        
                        print(f"[User - Interrupt] {inner_text}")
                        collected_sentences.append(inner_text)
                        
                        sleep_state["last_activity_time"] = time.time()
                        if sleep_state["is_sleeping"]:
                            sleep_state["is_sleeping"] = False
                            print("[Sleep] Waking up from sleep mode due to user speech")
                        
                        if not stt.is_muted:
                            reset_mute_timer()
                            
                        stt_cfg = get_stt_config()
                        if stt_cfg.get('aec_enabled', False):
                            tts_streamer.stop()

                    elif inner_event in ("endpoint", "autonomous_vision", "face_wake"):
                        if peeping_active and inner_event == "endpoint":
                            continue
                        
                        if inner_event == "face_wake":
                            if not vision_task_ref or vision_task_ref.done():
                                vision_task_ref = asyncio.create_task(vision_handler.run_vision_update(force_trigger=True))
                            continue
                        
                        stt_cfg = get_stt_config()
                        if stt_cfg.get('aec_enabled', False):
                            tts_streamer.stop()
                            stt_queue.put_nowait((inner_event, inner_text))
                            await gen_task
                            break

                active_tts_streamer = None

                # Strip movement tags from the response stored in history
                clean_response = strip_movement_tags(full_response.strip())

                # Remove the Big Brain injection from history (ephemeral)
                if bb_injection and message_history and message_history[-1].get("role") == "system":
                    # The injection was the last system message before LLM ran
                    # Actually, LLM may have added tool messages. Let's just keep it.
                    pass

                # 8. Add assistant response to history
                message_history.append({
                    "role": "assistant",
                    "content": clean_response
                })

                print(f"\n[Assistant] {clean_response}")
                print(f"{'─' * 40}\n")

                # 9. Execute pending movements (in background thread)
                if pending_movements:
                    movement_thread = threading.Thread(
                        target=execute_movements,
                        args=(pending_movements,),
                        daemon=True
                    )
                    movement_thread.start()
                    pending_movements = []

                # 10. Trigger Big Brain analysis (background async task)
                if get_big_brain_config().get("enabled", True):
                    # Cancel previous analysis if still running
                    if brain_task and not brain_task.done():
                        brain_task.cancel()

                    brain_task = asyncio.create_task(
                        analyze_conversation(
                            conversation_history=message_history,
                            past_conversation_summary=past_summary or "",
                            knowledge_summary=kb_summary or "",
                            available_tools=available_tools,
                            last_user_message=user_utterance,
                            last_ai_response=clean_response
                        )
                    )

                # 11. Trigger Vision Update (background async task)
                # Only trigger if another vision task isn't already running
                if not vision_task_ref or vision_task_ref.done():
                    vision_task_ref = asyncio.create_task(vision_handler.run_vision_update())

                # 11b. Fire after_response workers
                asyncio.create_task(worker_manager.fire_event("after_response"))

                # 12. Token counting and Auto-summarization
                current_tokens = token_counter.count_tokens(message_history, "gpt-5") #standard for tokenising.
                token_limit = agent_config.get("token_limit", 6000)
                print(f"[Chat Context] Token count: {current_tokens}/{token_limit}")

                if agent_config.get("auto_summarize", True) and not summarizing:
                    if current_tokens > token_limit:
                        print(f"\n[Summarization] Token limit exceeded ({current_tokens} > {token_limit}). Triggering async summary...")
                        summarizing = True
                        
                        async def summarize_task(current_history):
                            nonlocal message_history, summarizing
                            try:
                                # Prepare text for summarization
                                convo_text = "\n".join([f"{m['role'].upper()}: {m['content'] if isinstance(m['content'], str) else '[image]'}" for m in current_history[1:] if m.get("content") and m['role'] != 'system'])
                                sum_prompt = prompts_config.get("summarization_prompt", "Summarize this: {conversation}")
                                prompt = sum_prompt.format(conversation=convo_text)
                                
                                print("[Summarization] Generating summary...")
                                from core.brain.generate_llm_resp import generate
                                import concurrent.futures
                                loop = asyncio.get_running_loop()
                                with concurrent.futures.ThreadPoolExecutor() as pool:
                                    summary = await loop.run_in_executor(
                                        pool,
                                        lambda: generate(prompt, purpose="summary")
                                    )
                                    
                                if summary:
                                    print(f"[Summarization] Generated summary:\n{summary[:200]}...")
                                    save_summary_to_conversations_folder(summary)
                                    save_summary(summary)
                                    
                                    # Create new history keeping only the system prompt and injecting summary
                                    new_history = [message_history[0]] # Keep enriched prompt
                                    prev_sum_prompt = prompts_config.get("previous_summary_context", "Your memories:\n{summary}")
                                    
                                    nonlocal current_system_context
                                    current_system_context = prev_sum_prompt.format(summary=summary).strip()
                                    new_history.append({"role": "system", "content": current_system_context})
                                    message_history = new_history
                                    
                                    print("[Summarization] Context replaced with new summary")
                            except Exception as e:
                                print(f"[Summarization] Error: {e}")
                            finally:
                                summarizing = False
                                
                        asyncio.create_task(summarize_task(message_history.copy()))

                # 13. Unmute and start timeout
                #wait for 1 second before unmuting
                await asyncio.sleep(2)
                stt.unmute()
                reset_mute_timer()

    except KeyboardInterrupt:
        pass
    finally:
        cancel_mute_timer()
        sfx.stop()
        if livestream_process and livestream_process.poll() is None:
            print("[Main] Terminating livestream.py...")
            livestream_process.terminate()
            livestream_process.wait()
        # Fire shutdown workers (best-effort)
        try:
            await worker_manager.fire_event("shutdown")
        except Exception as e:
            print(f"[Main] Error firing shutdown workers: {e}")

        # Stop worker scheduler
        worker_manager.stop_scheduler()

        # Save conversation on exit
        try:
            convo_text = "\n".join([
                f"{m['role'].upper()}: {m['content'] if isinstance(m['content'], str) else '[image]'}"
                for m in message_history[1:] if m.get('content') and m['role'] != 'system'
            ])
            if convo_text.strip():
                print("[Main] Generating final conversation summary...")
                sum_prompt = prompts_config.get("summarization_prompt", "Summarize this: {conversation}")
                prompt = sum_prompt.format(conversation=convo_text)
                from core.brain.generate_llm_resp import generate
                summary = generate(prompt, purpose="summary")
                if summary:
                    print(f"[Summarization] Generated summary:\n{summary[:200]}...")
                    save_summary_to_conversations_folder(summary)
                    save_summary(summary)
                    print("[Main] Conversation summarized and saved.")
                else:
                    save_summary_to_conversations_folder(convo_text)
                    print("[Main] Summarization failed, raw conversation saved.")
        except Exception as e:
            print(f"[Main] Error saving conversation: {e}")

        # Cancel background tasks
        if brain_task and not brain_task.done():
            brain_task.cancel()
        face_task.cancel()
        periodic_q_task.cancel()
        if peeping_interval > 0:
            peeping_task.cancel()

        stt.stop()
        print("[Main] Goodbye!")
        os._exit(0)


if __name__ == "__main__":
    asyncio.run(main())
