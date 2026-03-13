"""
TTS module for KikiFast voice assistant.
Supports both Groq API and Inworld API.

Configured via tools_and_config/config.json using the "provider" key ("groq" or "inworld").
Client and config pre-initialized at import time for minimum latency.
"""

import os
import subprocess
import tempfile
import threading
from queue import Queue
import asyncio
import base64
import json
import time

import sys
import os


try:
    import websockets
except ImportError:
    websockets = None


try:
    from groq import Groq
except ImportError:
    Groq = None


from tools_and_config.config_loader import get_tts_config

# --- Pre-initialize at import time ---
_TTS_CFG = get_tts_config()
_tts_provider = _TTS_CFG.get("provider", "groq").lower()

_api_key = os.getenv("GROQ_API_KEY")
if not _api_key or not Groq:
    # Do not raise exception at import in case only inworld is needed
    _client = None
else:
    _client = Groq(api_key=_api_key)

_MODEL = _TTS_CFG.get("model", "canopylabs/orpheus-v1-english")
_VOICE = _TTS_CFG.get("voice", "daniel")
_FORMAT = _TTS_CFG.get("response_format", "wav")

_INWORLD_API_KEY = os.getenv("INWORLD_API_KEY", "")
_INWORLD_VOICE = _TTS_CFG.get("inworld_voice", "default-hj_w4b63okr5pruvwx2erq__danielgroq")
_INWORLD_MODEL = _TTS_CFG.get("inworld_model", "inworld-tts-1.5-mini")

print(f"[TTS] Pre-initialized: provider={_tts_provider}")


class GroqTTSStreamer:
    """
    Streaming TTS with pre-fetch queue using Groq.
    """
    def __init__(self):
        self._sentence_queue = Queue()         # sentences waiting to be fetched
        self._audio_queue = Queue(maxsize=2)   # pre-fetched audio files ready to play
        self._fetch_thread = None
        self._play_thread = None
        self._first_play_event = threading.Event()  # set when first audio starts playing
        self.interrupted = False
        if not _client:
            print("[TTS] GROQ_API_KEY not found!")

    def start(self):
        self._fetch_thread = threading.Thread(target=self._fetch_worker, daemon=True)
        self._play_thread = threading.Thread(target=self._play_worker, daemon=True)
        self._fetch_thread.start()
        self._play_thread.start()

    def add_sentence(self, text):
        self._sentence_queue.put(text)

    def finish(self):
        self._sentence_queue.put(None)
        if self._fetch_thread:
            self._fetch_thread.join()
        if self._play_thread:
            self._play_thread.join()

    def stop(self):
        self.interrupted = True
        import platform
        # Empty queues to unblock threads quickly
        while not self._sentence_queue.empty():
            try: self._sentence_queue.get_nowait()
            except: pass
        self._sentence_queue.put(None)
        
        while not self._audio_queue.empty():
            try: self._audio_queue.get_nowait()
            except: pass
        self._audio_queue.put(None)
        
        # Kill mpv
        try:
            if platform.system() == "Windows":
                 subprocess.Popen(["taskkill", "/F", "/IM", "mpv.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                 subprocess.Popen(["pkill", "-9", "mpv"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except:
            pass

    @property
    def first_play_event(self):
        return self._first_play_event

    def _fetch_worker(self):
        idx = 0
        while True:
            text = self._sentence_queue.get()
            if text is None:
                self._audio_queue.put(None)
                break

            idx += 1
            print(f"[TTS Fetch] Generating audio for sentence {idx}: {text[:40]}...")

            if not _client:
                continue

            fd, temp_path = tempfile.mkstemp(suffix=f".{_FORMAT}")
            os.close(fd)

            try:
                response = _client.audio.speech.create(
                    model=_MODEL,
                    voice=_VOICE,
                    input=text,
                    response_format=_FORMAT
                )
                response.write_to_file(temp_path)
                self._audio_queue.put(temp_path)
            except Exception as e:
                print(f"[TTS Fetch] Error on sentence {idx}: {e}")
                if os.path.exists(temp_path):
                    os.remove(temp_path)

    def _play_worker(self):
        idx = 0
        while True:
            audio_file = self._audio_queue.get()
            if audio_file is None:
                break

            idx += 1
            if not self._first_play_event.is_set():
                self._first_play_event.set()

            print(f"[TTS Play] Playing sentence {idx}")
            try:
                subprocess.run(
                    ["mpv", "--no-video", audio_file],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            except Exception as e:
                print(f"[TTS Play] Playback error: {e}")
            finally:
                if os.path.exists(audio_file):
                    os.remove(audio_file)

        print("[TTS Play] All sentences played")


class InworldTTSStreamer:
    """
    Streaming TTS using Inworld WebSocket for lowest TTFB.
    """
    def __init__(self):
        self._first_play_event = threading.Event()
        self._sentence_queue = None
        self._loop = None
        self._thread = None
        self.interrupted = False

    @property
    def first_play_event(self):
        return self._first_play_event

    def start(self):
        if not websockets:
            print("[TTS Inworld] Error: websockets not installed.")
            return
            
        self._ready_event = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready_event.wait()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._sentence_queue = asyncio.Queue()
        self._ready_event.set()
        
        try:
            self._loop.run_until_complete(self._websocket_tts())
        except Exception as e:
            print(f"[TTS Inworld] Error in loop: {e}")
        finally:
            self._loop.close()

    def add_sentence(self, text):
        if self._loop and self._sentence_queue:
            self._loop.call_soon_threadsafe(self._sentence_queue.put_nowait, text)

    def finish(self):
        if self._loop and self._sentence_queue:
            self._loop.call_soon_threadsafe(self._sentence_queue.put_nowait, None)
        if self._thread:
            self._thread.join()

    def stop(self):
        self.interrupted = True
        import platform
        if self._loop and self._sentence_queue:
            self._loop.call_soon_threadsafe(self._sentence_queue.put_nowait, None)
        try:
            if platform.system() == "Windows":
                 subprocess.Popen(["taskkill", "/F", "/IM", "mpv.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                 subprocess.Popen(["pkill", "-9", "mpv"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except:
            pass

    async def _websocket_tts(self):
        url = "wss://api.inworld.ai/tts/v1/voice:streamBidirectional"
        headers = {"Authorization": f"Basic {_INWORLD_API_KEY}"}
        context_id = f"ctx-{time.time()}"
        
        try:
            async with websockets.connect(url, additional_headers=headers) as ws:
                create_msg = {
                    "context_id": context_id,
                    "create": {
                        "voice_id": _INWORLD_VOICE,
                        "model_id": _INWORLD_MODEL,
                        "audio_config": {
                            "audio_encoding": "OGG_OPUS",
                            "sample_rate_hertz": 24000,
                            "bit_rate": 32000
                        }
                    }
                }
                await ws.send(json.dumps(create_msg))
                
                # Wait for context creation
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if "error" in data:
                        print(f"[TTS Inworld] Error creating context: {data['error']}")
                        return
                    if "contextCreated" in data.get("result", {}):
                        break

                print("[TTS Inworld] Context created. Starting streaming audio play...")
                
                mpv_process = await asyncio.create_subprocess_exec(
                    "mpv", "--no-video", "--untimed", "--audio-pitch-correction=no", "--cache-pause=no", "-",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )

                async def receive_audio():
                    async for message in ws:
                        data = json.loads(message)
                        if "error" in data:
                            print(f"[TTS Inworld] Error receiving: {data['error']}")
                            break
                        
                        result = data.get("result")
                        if not result:
                            if data.get("done"):
                                break
                            continue
                            
                        if "contextClosed" in result:
                            break
                            
                        if "audioChunk" in result:
                            if not self._first_play_event.is_set():
                                self._first_play_event.set()
                                
                            b64_content = result["audioChunk"].get("audioContent")
                            if b64_content:
                                audio_bytes = base64.b64decode(b64_content)
                                if mpv_process.returncode is None:
                                    try:
                                        mpv_process.stdin.write(audio_bytes)
                                        await mpv_process.stdin.drain()
                                    except (BrokenPipeError, ConnectionResetError):
                                        pass

                recv_task = asyncio.create_task(receive_audio())

                try:
                    while True:
                        text = await self._sentence_queue.get()
                        if text is None:
                            break
                        
                        print(f"[TTS Inworld] Sending text: {text[:40]}...")
                        text_msg = {
                            "context_id": context_id,
                            "send_text": {
                                "text": text,
                                "flush_context": {}
                            }
                        }
                        await ws.send(json.dumps(text_msg))
                finally:
                    close_msg = {"context_id": context_id, "close_context": {}}
                    try:
                        await ws.send(json.dumps(close_msg))
                    except:
                        pass
                    
                    await recv_task
                    
                    if mpv_process.returncode is None:
                        try:
                            mpv_process.stdin.close()
                        except:
                            pass
                        await mpv_process.wait()

        except Exception as e:
            print(f"[TTS Inworld] WebSocket connection error: {e}")


def TTSStreamer():
    """Factory function returning the configured TTSStreamer."""
    if _tts_provider == "inworld":
        return InworldTTSStreamer()
    else:
        return GroqTTSStreamer()


def speak_sentence(text):
    """Simple blocking TTS for a single sentence."""
    if _tts_provider == "inworld":
        streamer = InworldTTSStreamer()
        streamer.start()
        streamer.add_sentence(text)
        streamer.finish()
        return

    if not _client:
        print("[TTS] Cannot speak, Groq client not initialized")
        return
        
    fd, temp_path = tempfile.mkstemp(suffix=f".{_FORMAT}")
    os.close(fd)
    try:
        response = _client.audio.speech.create(
            model=_MODEL, voice=_VOICE, input=text, response_format=_FORMAT
        )
        response.write_to_file(temp_path)
        subprocess.run(
            ["mpv", "--no-video", temp_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception as e:
        print(f"[TTS] Error: {e}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


if __name__ == "__main__":
    print("=== TTS Streamer Test ===")
    streamer = TTSStreamer()
    streamer.start()
    streamer.add_sentence("Hello! This is the first sentence.")
    streamer.add_sentence("And this is the second sentence, generated quickly.")
    streamer.finish()
    print("Done.")
