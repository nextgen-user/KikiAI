"""
Kiki Voice Assistant — Setup Script
====================================

Cross-platform installer that:
  1. Checks Python version
  2. Installs pip requirements
  3. Auto-installs mpv, ffmpeg, and yt-dlp
  4. Creates .env from .env.example if needed
  5. Creates required directories
  6. Validates the setup
"""

import os
import sys
import shutil
import subprocess
import platform

# Colors for terminal output
class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"

def ok(msg):     print(f"{Colors.GREEN}  ✓ {msg}{Colors.END}")
def warn(msg):   print(f"{Colors.YELLOW}  ⚠ {msg}{Colors.END}")
def fail(msg):   print(f"{Colors.RED}  ✗ {msg}{Colors.END}")
def info(msg):   print(f"{Colors.CYAN}  → {msg}{Colors.END}")
def header(msg): print(f"\n{Colors.BOLD}{msg}{Colors.END}")


def check_python():
    header("1. Checking Python version...")
    v = sys.version_info
    if v.major >= 3 and v.minor >= 10:
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
        return True
    else:
        fail(f"Python {v.major}.{v.minor}.{v.micro} — need 3.10+")
        return False


def install_pip_requirements():
    header("2. Installing Python packages...")
    req_file = os.path.join(os.path.dirname(__file__), "requirements.txt")
    if not os.path.exists(req_file):
        fail("requirements.txt not found!")
        return False
    try:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "-r", req_file, "--quiet"
        ])
        ok("All Python packages installed")
        return True
    except subprocess.CalledProcessError as e:
        fail(f"pip install failed: {e}")
        return False


def check_and_install_tool(name, install_cmds):
    """Check if a CLI tool exists, try to install if missing."""
    if shutil.which(name):
        ok(f"{name} is installed ({shutil.which(name)})")
        return True

    warn(f"{name} not found, attempting to install...")
    system = platform.system()

    cmds = install_cmds.get(system)
    if not cmds:
        fail(f"Don't know how to install {name} on {system}")
        return False

    for cmd in cmds:
        try:
            info(f"Running: {cmd}")
            subprocess.check_call(cmd, shell=True)
            if shutil.which(name):
                ok(f"{name} installed successfully")
                return True
        except subprocess.CalledProcessError:
            continue

    fail(f"Could not install {name}. Please install it manually:")
    manual = {
        "mpv":   "https://mpv.io/installation/",
        "ffmpeg": "https://ffmpeg.org/download.html",
        "yt-dlp": "pip install yt-dlp"
    }
    info(manual.get(name, f"Search: 'install {name} {system}'"))
    return False


def install_external_tools():
    header("3. Checking external tools...")

    # mpv — required for audio playback
    check_and_install_tool("mpv", {
        "Windows": ["winget install --id=mpv-player.mpv -e --accept-source-agreements"],
        "Darwin":  ["brew install mpv"],
        "Linux":   ["sudo apt-get install -y mpv", "sudo pacman -S --noconfirm mpv"],
    })

    # ffmpeg — required for audio processing
    check_and_install_tool("ffmpeg", {
        "Windows": ["winget install --id=Gyan.FFmpeg -e --accept-source-agreements"],
        "Darwin":  ["brew install ffmpeg"],
        "Linux":   ["sudo apt-get install -y ffmpeg", "sudo pacman -S --noconfirm ffmpeg"],
    })

    # yt-dlp — required for play_music tool
    if not shutil.which("yt-dlp"):
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "yt-dlp", "--quiet"])
            ok("yt-dlp installed via pip")
        except subprocess.CalledProcessError:
            warn("yt-dlp not installed — play_music tool won't work")


def setup_env_file():
    header("4. Setting up environment...")
    project_dir = os.path.dirname(__file__)
    env_file = os.path.join(project_dir, ".env")
    example_file = os.path.join(project_dir, ".env.example")

    if os.path.exists(env_file):
        ok(".env file already exists")
    elif os.path.exists(example_file):
        shutil.copy2(example_file, env_file)
        ok("Created .env from .env.example")
        warn("⚠  You MUST fill in your API keys in .env before running Kiki!")
    else:
        fail(".env.example not found — cannot create .env")


def create_directories():
    header("5. Creating required directories...")
    project_dir = os.path.dirname(__file__)
    dirs = ["conversations"]
    for d in dirs:
        path = os.path.join(project_dir, d)
        os.makedirs(path, exist_ok=True)
        ok(f"Directory: {d}/")


def validate_setup():
    header("6. Validating setup...")
    all_ok = True

    # Check .env has been configured
    project_dir = os.path.dirname(__file__)
    env_file = os.path.join(project_dir, ".env")
    if os.path.exists(env_file):
        with open(env_file, 'r') as f:
            content = f.read()
        if "your_deepgram_api_key_here" in content:
            warn(".env has placeholder keys — fill them in before running!")
        else:
            ok(".env appears configured")
    else:
        fail(".env file missing")
        all_ok = False

    # Check mpv
    if shutil.which("mpv"):
        ok("mpv is available")
    else:
        fail("mpv not found — audio playback won't work")
        all_ok = False

    # Check mic access
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        device_count = pa.get_device_count()
        input_devices = []
        for i in range(device_count):
            dev = pa.get_device_info_by_index(i)
            if dev.get("maxInputChannels", 0) > 0:
                input_devices.append(f"  [{i}] {dev['name']}")
        pa.terminate()
        if input_devices:
            ok(f"Found {len(input_devices)} microphone(s)")
            for d in input_devices[:5]:
                info(d)
        else:
            warn("No microphone input devices found")
    except ImportError:
        warn("pyaudio not installed — can't check microphone")
    except Exception as e:
        warn(f"Could not check microphone: {e}")

    return all_ok


def main():
    print(f"\n{Colors.BOLD}{'='*50}")
    print(f"  Kiki Voice Assistant — Setup")
    print(f"{'='*50}{Colors.END}")

    if not check_python():
        sys.exit(1)

    install_pip_requirements()
    install_external_tools()
    setup_env_file()
    create_directories()
    success = validate_setup()

    print(f"\n{Colors.BOLD}{'='*50}{Colors.END}")
    if success:
        ok("Setup complete! Run Kiki with: python main.py")
    else:
        warn("Setup complete with warnings. Review issues above.")
    print()


if __name__ == "__main__":
    main()
