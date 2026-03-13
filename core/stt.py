"""
STT module for KikiFast voice assistant.
Wraps Deepgram streaming transcription with mute/unmute support.
"""

import threading
import queue
import os
import pyaudio
from deepgram import DeepgramClient
from deepgram.core.events import EventType
from tools_and_config.config_loader import get_stt_config


class STTEngine:
    """
    Streaming speech-to-text engine using Deepgram.
    Supports mute/unmute to prevent speaker feedback during TTS playback.
    """

    def __init__(self):
        cfg = get_stt_config()
        self.device_index = cfg["device_index"]
        self.channels = cfg["channels"]
        self.sample_rate = cfg["sample_rate"]
        self.chunk_size = cfg["chunk_size"]
        self.model = cfg["model"]
        self.endpointing_ms = cfg["endpointing_ms"]
        self.utterance_end_ms = cfg["utterance_end_ms"]

        self._muted = threading.Event()  # When set, audio is NOT sent
        self._stop = threading.Event()
        self._evt_queue = queue.Queue()
        self._audio = None
        self._stream = None

    def mute(self):
        """Mute the microphone (stop sending audio to Deepgram)."""
        self._muted.set()
        print("[STT] 🔇 Mic muted")

    def unmute(self):
        """Unmute the microphone (resume sending audio to Deepgram)."""
        self._muted.clear()
        print("[STT] 🔊 Mic unmuted")

    @property
    def is_muted(self):
        return self._muted.is_set()

    def stream(self):
        """
        Generator that yields transcription events.

        Yields:
            tuple: (event_type, data)
                   event_type: 'final' | 'endpoint'
                   data: transcript string for 'final', None for 'endpoint'
        """
        dg_api_key = os.getenv("DEEPGRAM_API_KEY") or os.getenv("DEEPGRAM")
        if not dg_api_key:
            raise ValueError("DEEPGRAM_API_KEY or DEEPGRAM not found in environment!")

        deepgram = DeepgramClient()

        with deepgram.listen.v1.connect(
            model=self.model,
            smart_format="true",
            encoding="linear16",
            sample_rate=str(self.sample_rate),
            channels=str(self.channels),
            interim_results="true",
            utterance_end_ms=self.utterance_end_ms,
            vad_events="true",
            endpointing=self.endpointing_ms
        ) as connection:

            def on_message(message):
                if hasattr(message, 'channel') and hasattr(message.channel, 'alternatives'):
                    sentence = message.channel.alternatives[0].transcript
                    if len(sentence) > 0:
                        is_final = getattr(message, "is_final", False)
                        if is_final:
                            self._evt_queue.put(("final", sentence))
                        else:
                            print(f"[STT] Interim: {sentence}")

                    if getattr(message, "speech_final", False):
                        self._evt_queue.put(("endpoint", None))

            def on_error(error):
                self._evt_queue.put(("error", str(error)))

            connection.on(EventType.MESSAGE, on_message)
            connection.on(EventType.ERROR, on_error)

            # Start Deepgram listener
            listen_thread = threading.Thread(target=connection.start_listening)
            listen_thread.daemon = True
            listen_thread.start()

            # PyAudio setup
            self._audio = pyaudio.PyAudio()
            self._stream = self._audio.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=self.chunk_size
            )

            # Pre-compute a silent frame (all zeros) for muted mode
            # This keeps the Deepgram connection alive and prevents 1011 timeout
            silence = b'\x00' * (self.chunk_size * self.channels * 2)  # 16-bit = 2 bytes per sample

            # Audio feeder thread — reads mic and sends to Deepgram
            def audio_feeder():
                try:
                    while not self._stop.is_set():
                        data = self._stream.read(self.chunk_size, exception_on_overflow=False)
                        if self._muted.is_set():
                            # Send silence to keep connection alive
                            connection.send_media(silence)
                        else:
                            connection.send_media(data)
                except Exception as e:
                    if not self._stop.is_set():
                        self._evt_queue.put(("error", f"Audio stream error: {e}"))
                finally:
                    try:
                        connection.send_close()
                    except Exception:
                        pass

            feeder_thread = threading.Thread(target=audio_feeder)
            feeder_thread.daemon = True
            feeder_thread.start()

            try:
                while not self._stop.is_set():
                    try:
                        evt_type, data = self._evt_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if evt_type == "error":
                        print(f"[STT] Error: {data}")
                        break
                    yield evt_type, data
            except KeyboardInterrupt:
                pass
            finally:
                self.stop()

    def stop(self):
        """Stop the STT engine and clean up resources."""
        self._stop.set()
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
        if self._audio:
            try:
                self._audio.terminate()
            except Exception:
                pass
        print("[STT] Engine stopped")


if __name__ == "__main__":
    print("=== STT Module Test ===")
    print("Listening... Press Ctrl+C to stop.\n")

    engine = STTEngine()
    collected = []

    try:
        for event, text in engine.stream():
            if event == "final":
                print(f"  Final: {text}")
                collected.append(text)
            elif event == "endpoint":
                if collected:
                    utterance = " ".join(collected)
                    print(f"\n>>> [ENDPOINT] Full: {utterance}\n")
                    collected = []
    except KeyboardInterrupt:
        engine.stop()
