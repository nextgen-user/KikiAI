import asyncio
import time
from collections import deque
from tools_and_config.config_loader import get_full_config
from core.stt import STTEngine

async def face_event_listener(message_history: list, stt: STTEngine, sleep_state: dict, loop: asyncio.AbstractEventLoop, stt_queue: asyncio.Queue, face_history=None):
    """Listen for face events from KikiController and inject into chat history."""
    
    while True: # Robust retry loop
        config = get_full_config()
        face_config = config.get("face_events", {})

        if not face_config.get("enabled", False):
            print("[Face] Face events disabled in config. Checking again in 60s...")
            await asyncio.sleep(60)
            continue

        # Track last 2 events timestamps for 2-per-5-min rate limit
        face_event_timestamps = deque()

        try:
            from kiki_control_client import KikiController
            ctrl_config = config.get("controller", {})
            controller = KikiController(host=ctrl_config.get("host", "192.168.1.11"))

            connected = await controller.connect()
            if not connected:
                print("[Face] Failed to connect to KikiController. Retrying in 10s...(Ignore error on Windows Edition - #TODO: Fix this)")
                await asyncio.sleep(10)
                continue

            print("[Face] Connected to KikiController, listening for face events...")

            async for event in controller.listen_events():
                print(f"[Face] Received event from controller: {event}")
                event_type = event.get("event")
                if event_type in ["face_detected", "face_lost"]:
                    person_name = event.get("person", event.get("name", "Unknown"))
                    print(f"[Face] Processing {event_type} for {person_name}")
                    now = time.time()

                    # Record face event in shared history buffer (for Workers)
                    if face_history is not None:
                        face_history.record_face(
                            person_name,
                            "detected" if event_type == "face_detected" else "lost"
                        )

                    # Fire face_detected workers
                    if event_type == "face_detected":
                        try:
                            from core.workers.worker_manager import get_worker_manager
                            manager = get_worker_manager()
                            asyncio.create_task(
                                manager.fire_event("face_detected", person=person_name)
                            )
                        except Exception as e:
                            print(f"[Face] Error firing face_detected workers: {e}")

                    # Clean up timestamps older than 5 minutes (300 seconds)
                    while face_event_timestamps and now - face_event_timestamps[0] > 300:
                        face_event_timestamps.popleft()

                    if len(face_event_timestamps) >= 2:
                        print(f"[Face] Rate limit (2 msgs / 5 mins) exceeded. Skipping injection.")
                        continue

                    face_event_timestamps.append(now)

                    if event_type == "face_detected":
                        if person_name != "Unknown":
                            # Known face wakes up from sleep
                            if sleep_state.get("is_sleeping", False):
                                sleep_state["is_sleeping"] = False
                                print(f"[Face] Known face {person_name} woke robot from sleep")
                                # Trigger a vision update immediately to start the proactive chain
                                await stt_queue.put(("face_wake", person_name))
                                
                            sleep_state["last_activity_time"] = time.time()

                        msg = f"[System: The known person '{person_name}' has just appeared in front of Kiki.]"
                    else:
                        msg = f"[System: The known person '{person_name}' has left Kiki's view.]"

                    print(f"[Face] Injecting message: {msg}")

                    # Inject into message history as a system event
                    message_history.append({
                        "role": "system",
                        "content": msg
                    })

        except ImportError:
            print("[Face] kiki_control_client not available")
            break # No point retrying if file is missing
        except Exception as e:
            print(f"[Face] Event listener error: {e}. Retrying in 5s...")
            await asyncio.sleep(5)
