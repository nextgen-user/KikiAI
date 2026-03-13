import os
import struct
import pyaudio
import pvporcupine
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class HotwordRecognizer:
    def __init__(self, keyword_paths, access_key=None, device_index=2):
        """
        Initialize the Hotword Recognizer using Picovoice Porcupine.
        
        :param keyword_paths: List of paths to .ppn files.
        :param access_key: Picovoice Access Key. If None, it tries to load from .env.
        :param device_index: PyAudio input device index.
        """
        self.access_key = access_key or os.getenv("PICOVOICE_ACCESS_KEY")
        if not self.access_key:
            raise ValueError("Picovoice Access Key not found. Set PICOVOICE_ACCESS_KEY in .env or pass it to the constructor.")
        
        self.keyword_paths = keyword_paths
        self.device_index = device_index
        
        # Extract keyword names from paths for logging
        self.keyword_names = [os.path.splitext(os.path.basename(p))[0] for p in keyword_paths]
        
        # Initialize Porcupine
        try:
            self.porcupine = pvporcupine.create(
                access_key=self.access_key,
                keyword_paths=self.keyword_paths
            )
        except Exception as e:
            print(f"Error initializing Porcupine: {e}")
            raise

        self.pa = pyaudio.PyAudio()
        self.audio_stream = self.pa.open(
            rate=self.porcupine.sample_rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=self.porcupine.frame_length,
            input_device_index=self.device_index
        )

    def listen(self):
        """
        Generator that yields detected hotword names.
        """
        print(f"Listening for hotwords: {', '.join(self.keyword_names)}")
        try:
            while True:
                pcm = self.audio_stream.read(self.porcupine.frame_length, exception_on_overflow=False)
                pcm = struct.unpack_from("h" * self.porcupine.frame_length, pcm)

                keyword_index = self.porcupine.process(pcm)
                if keyword_index >= 0:
                    detected_word = self.keyword_names[keyword_index]
                    print(f"\n[Hotword] Detected: {detected_word}")
                    yield detected_word
                    
        except KeyboardInterrupt:
            print("\nStopping Hotword Recognizer...")
        finally:
            self.cleanup()

    def cleanup(self):
        """Clean up resources."""
        if hasattr(self, 'audio_stream') and self.audio_stream is not None:
            self.audio_stream.close()
        if hasattr(self, 'pa') and self.pa is not None:
            self.pa.terminate()
        if hasattr(self, 'porcupine') and self.porcupine is not None:
            self.porcupine.delete()

def main():
    # Paths to the hotword files (relative to this script's directory)
    base_path = os.path.dirname(os.path.abspath(__file__))
    keywords = [
        os.path.join(base_path, f)
        for f in os.listdir(base_path)
        if f.endswith(".ppn")
    ]
    
    if not keywords:
        print("Error: No .ppn hotword files found in", base_path)
        return
    
    # Check if files exist
    for kw in keywords:
        if not os.path.exists(kw):
            print(f"Warning: Hotword file not found at {kw}")

    recognizer = HotwordRecognizer(keyword_paths=keywords, device_index=None)
    
    try:
        for hotword in recognizer.listen():
            # Example logic: Do something based on the hotword
            if hotword == "stop_it":
                print("Action: Stopping system...")
            elif hotword == "heyy":
                print("Action: Greeting user!")
            elif hotword == "stop_music":
                print("Action: Stopping music...")
    except Exception as e:
        print(f"Error in main loop: {e}")

if __name__ == "__main__":
    main()
