import asyncio
import time
import random
from core.vision.camera import capture_photo_b64
from core.brain.generate_llm_resp import generate as generate_llm
from pathlib import Path

target_path = Path("~/Kiki/hailo-apps/hailo_apps/python/pipeline_apps/face_recognition/train")
target_path = target_path.expanduser()

class VisionHandler:
    def __init__(self, config, loop, stt_queue, sleep_checker):
        self.config = config
        self.loop = loop
        self.stt_queue = stt_queue
        self.sleep_checker = sleep_checker

        prompts_config = config.get("prompts", {})
        agent_config = config.get("agent", {})
        # Face recognition training path (Raspberry Pi / Hailo only)
        # On Windows/non-robot setups, this path won't exist — that's fine
        try:
            known_people = [item.name for item in target_path.iterdir() if item.is_dir()] if target_path.exists() else []
        except Exception:
            known_people = []
        
        self.vision_prefix = prompts_config.get("vision_update", {}).get("prefix", "[WHAT KIKI SEES]: ")
        self.vision_prompt = prompts_config.get("vision_update", {}).get("prompt", "Describe what you see.").format(known_people=known_people)
        
        self.question_min_interval = agent_config.get("question_min_interval_seconds", 120)
        self.question_max_interval = agent_config.get("question_max_interval_seconds", 180)
        
        self.last_question_time = time.time()
        self.next_question_interval = random.randint(
            min(self.question_min_interval, self.question_max_interval), 
            max(self.question_min_interval, self.question_max_interval)
        )
        self.pending_vision_context = None

    async def run_vision_update(self, force_trigger=False):
        if self.sleep_checker():
            print("[Vision] Skipped - Kiki is sleeping.")
            return

        vision_injection_cfg = self.config.get("vision_injection", {})
        traditional_enabled = vision_injection_cfg.get("traditional_context_enabled", True)
        
        now = time.time()
        elapsed = now - self.last_question_time
        is_periodic_time = elapsed >= self.next_question_interval
        
        if not traditional_enabled and not is_periodic_time and not force_trigger:
            # print("[Vision] Skipped - traditional vision context is disabled via config.")
            return

        try:
            print("[Vision] Triggering vision capture (post-LLM response)...")
            b64_image = await self.loop.run_in_executor(None, capture_photo_b64)
            if not b64_image:
                print("[Vision] Error: No image returned by camera.")
                return
            
            def _analyze():
                return generate_llm(self.vision_prompt, b64_image=b64_image, purpose="vision")
            
            analysis_result = await self.loop.run_in_executor(None, _analyze)
            if not analysis_result:
                print("[Vision] Failed to get vision analysis from LLM")
                return
                
            full_vision_context = f"{self.vision_prefix}{analysis_result}"
            print(f"[Vision] Context stored: {full_vision_context[:100]}...")
            
            # Record into shared vision history (for Workers)
            try:
                from core.workers.worker_brain import get_vision_history
                get_vision_history().record_vision(full_vision_context)
            except Exception:
                pass  # Workers module may not be initialized yet
            
            now = time.time()
            elapsed = now - self.last_question_time
            if elapsed >= self.next_question_interval or force_trigger:
                print(f"[Vision] Periodic/forced interval reached. Queuing autonomous event.")
                self.last_question_time = now
                self.next_question_interval = random.randint(
                    min(self.question_min_interval, self.question_max_interval), 
                    max(self.question_min_interval, self.question_max_interval)
                )
                
                await self.stt_queue.put(("autonomous_vision", full_vision_context))
            else:
                if traditional_enabled:
                    self.pending_vision_context = full_vision_context
                
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[Vision] Error: {e}")
