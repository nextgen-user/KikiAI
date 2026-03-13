"""
Thinking sound effects player for KikiFast voice assistant.
Loops through thinking sounds while the LLM is generating a response.
"""

import os
import random
import subprocess
import threading
from tools_and_config.config_loader import get_sfx_config


class ThinkingSoundPlayer:
    """
    Plays thinking/waiting sounds in a loop on a background thread.
    Sounds are played sequentially (shuffled) and loop until stop() is called.
    """

    def __init__(self):
        cfg = get_sfx_config()
        base_dir = os.path.dirname(os.path.abspath(__file__))
        sfx_dir = os.path.join(base_dir, cfg["directory"])

        self._files = [
            os.path.join(sfx_dir, f)
            for f in cfg["thinking_files"]
            if os.path.exists(os.path.join(sfx_dir, f))
        ]

        if not self._files:
            print("[SFX] Warning: No thinking sound files found!")

        self._stop_event = threading.Event()
        self._thread = None
        self._process = None
        self._lock = threading.Lock()

    def start(self):
        """Start playing thinking sounds in a loop."""
        if self._thread and self._thread.is_alive():
            return  # Already playing

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._play_loop, daemon=True)
        self._thread.start()
        print("[SFX] 🎵 Thinking sounds started")

    def stop(self):
        """Stop playing thinking sounds."""
        self._stop_event.set()

        # Kill current playback process
        with self._lock:
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._process.kill()

        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

        print("[SFX] 🔇 Thinking sounds stopped")

    def _play_loop(self):
        """Background loop that plays sounds until stopped."""
        cfg = get_sfx_config()
        delay = cfg.get("delay_between_files_seconds", 0.0)

        while not self._stop_event.is_set():
            # Shuffle the files each cycle for variety
            files = self._files.copy()
            random.shuffle(files)

            for sound_file in files:
                if self._stop_event.is_set():
                    break

                try:
                    with self._lock:
                        self._process = subprocess.Popen(
                            ["mpv", "--no-video", sound_file],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL
                        )

                    # Wait for playback to finish or stop signal
                    while self._process.poll() is None:
                        if self._stop_event.is_set():
                            self._process.terminate()
                            try:
                                self._process.wait(timeout=1)
                            except subprocess.TimeoutExpired:
                                self._process.kill()
                            return
                        self._stop_event.wait(timeout=0.1)

                    # Adding the configurable delay cleanly, without blocking if stopped
                    if delay > 0 and not self._stop_event.is_set():
                        self._stop_event.wait(timeout=delay)

                except Exception as e:
                    print(f"[SFX] Playback error: {e}")

    @property
    def is_playing(self):
        return self._thread is not None and self._thread.is_alive()


if __name__ == "__main__":
    import time
    print("=== Sound Effects Test ===")
    player = ThinkingSoundPlayer()
    player.start()
    time.sleep(5)
    player.stop()
    print("Done.")
