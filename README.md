# 🤖 Kiki — Your AI Friend

Kiki is an open-source **AI companion** that sees, hears, speaks, remembers, and cares. Unlike voice assistants that wait for commands, Kiki proactively starts conversations, notices when you're stressed, shares interesting news, and remembers your life — like a real friend who happens to have wheels, cameras, and a silicon brain.

## 💡 Why Kiki?

Audio-to-audio models are still too limited in capability, and voice agent platforms like LiveKit introduce unnecessary complexity and latency. Kiki takes a different approach.

**Dual-brain architecture:** Kiki uses a lightweight LLM to respond instantly, while simultaneously thinking deeper in the background after each response — just like a human who keeps reflecting after they've already spoken.

**Built to be a friend, not just an assistant.** Most AI is designed to complete tasks on demand. Kiki is one of the rare AI companions built around genuine companionship. Just start it up in the morning, say *"Hey Kiki"*, and dive into a question, a worry, or just a fun chat — whenever you feel like it.

**Raspberry Pi roots.** Kiki was originally built as a Raspberry Pi-based robot, using the [Hailo AI HAT](https://hailo.ai/) (RPI AI HAT) for real-time face recognition, tracking, and person-following directly on-device. The current release is a **Windows edition** — the RPi robot edition is coming soon.

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🎤 **Real-time voice conversation** | Deepgram STT → LLM → Groq/Inworld TTS pipeline with sub-second latency |
| 🧠 **Long-term memory** | Remembers people, facts, conversations, personality traits across sessions |
| 👁️ **Vision** | Camera + screen capture for observing the environment (optional) |
| 🤔 **Big Brain** | Background analysis that suggests proactive conversation topics |
| ⏰ **Autonomous workers** | Scheduled/event-driven background tasks (reminders, recurring checks) |
| 🎵 **Music playback** | YouTube search and playback via yt-dlp + mpv |
| 🔍 **Web search** | Real-time web search for current events (Exa API) |
| 🗣️ **Hotword detection** | "Hey Kiki" wake word via Picovoice Porcupine |
| 🤖 **Robot control** | Motor & face tracking for physical robot builds (optional) (coming soon) |

## 🚀 Quick Start

### 1. Clone & Setup

```bash
git clone https://github.com/yourusername/KikiFast.git
cd KikiFast
python setup_kiki.py
```

The setup script will:
- Install Python packages from `requirements.txt`
- Auto-install `mpv` and `ffmpeg` (via winget/brew/apt)
- Create `.env` from template
- Validate your microphone and setup

### 2. Configure API Keys

Edit `.env` and fill in your API keys:

```bash
# Required
DEEPGRAM_API_KEY=...          # Speech-to-Text

GOOGLE_APPLICATION_CREDENTIALS=.. #preferred - low latency else GEMINI_API_KEY=..  (for responses)

GEMINI_KEY_LIST=["key1"]      # LLM (for deep thinking) ( (multiple used to bypass rate limits - free tier)
 
GROQ_API_KEY=...              # TTS  

PICOVOICE_ACCESS_KEY=...      # Wake word detection 

EXA_API_KEY=...               # Web search (exa.ai)

#Optional
GROQ_KEY_LIST=["key1","key2"] #Fallback LLM for thinking
INWORLD_API_KEY=.. #change provider to "inworld" tts as well in config.json
```

### 3. Personalize Kiki

```bash
python about_person.py
```

This interactive script asks you about yourself — your name, interests, humor preferences, daily routine, people in your life — and configures Kiki's entire personality to be **your** friend. It configures all system prompts,  initializes knowledge base,etc.

### 4. Run

```bash
python main.py
```

Say **"Hey Kiki"** to wake, then talk naturally!

## 📁 Project Structure

```
KikiFast/
├── main.py                    # Main orchestrator
├── paths.py                   # Central path resolution (PROJECT_ROOT)
├── setup_kiki.py              # Cross-platform installer
├── about_person.py            # Personalization wizard (run once)
├── person_profile.json        # Your saved personality profile
├── livestream.py              # Desktop MJPEG stream for vision
├── core/
│   ├── stt.py                 # Speech-to-Text (Deepgram streaming)
│   ├── tts.py                 # Text-to-Speech (Groq / Inworld)
│   ├── llm.py                 # LLM routing (Gemini, Groq, local)
│   ├── brain/
│   │   ├── big_brain.py       # Background conversation analysis
│   │   ├── knowledge_base.py  # Persistent memory system
│   │   ├── summary_manager.py # Conversation summarization
│   │   └── generate_llm_resp.py
│   ├── vision/
│   │   ├── camera.py          # Image capture from MJPEG stream
│   │   └── vision_handler.py  # Environment observation
│   └── workers/
│       ├── worker_manager.py  # Task scheduler
│       └── worker_brain.py    # Autonomous task executor
├── hotwords/                  # Porcupine .ppn wake word models
├── sound_effects/             # Audio feedback files
├── tools_and_config/
│   ├── config.json            # All configuration
│   ├── config_loader.py       # Config parser
│   └── tools.py               # Tool implementations (search, music, timer, etc.)
└── robot/                     # Hardware control (optional)
    ├── face_handler.py
    └── movement.py
```

## ⚙️ Configuration

All settings live in `tools_and_config/config.json`:

- **LLM**: Model selection, temperature, system prompt personality
- **TTS**: Provider (groq/inworld), voice, format
- **STT**: Microphone device index (null = auto-detect), model
- **Big Brain**: Background analysis frequency and depth
- **Workers**: Autonomous task scheduling
- **Vision**: Camera integration settings

### Microphone Setup

If Kiki picks up the wrong microphone, set `stt.device_index` in config.json. Run `setup_kiki.py` to see available devices and their indices.

## 🏗️ Architecture

```
┌─────────────────────────────────────────────  ┐
│                  main.py                      │
│  (orchestrator: hotword → STT → LLM → TTS)    │
├────────┬───────────┬────────────┬──────────── ┤
│ Hotword│    STT    │    LLM     │    TTS      │
│Porcupin│ Deepgram  │ Gemini/    │ Groq/       │
│  .ppn  │ streaming │ Groq/Local │ Inworld     │
├────────┴───────────┴────────────┴──────────── ┤
│              Background Tasks                 │
│ Big Brain │ Workers │ Vision │Face Events(RPI)│
├───────────────────────────────────────────────┤
│         Knowledge Base (JSON memory)          │
└───────────────────────────────────────────────┘
```

## 🗺️ Roadmap

- [ ] MCP and Claude Skills support
- [ ] Laptop control integration
- [ ] Expanded provider support (LLMs, TTS, STT)
- [ ] Turn interruption and Acoustic Echo Cancellation (AEC)
- [ ] Transparent overlay UI window
- [ ] Raspberry Pi robot edition release
- [ ] Comprehensive documentation and setup guides
## 🤝 Contributing

Contributions welcome!

## 📄 License

[MIT License](LICENSE)
