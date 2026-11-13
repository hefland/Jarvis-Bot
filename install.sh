#!/usr/bin/env bash
# One-time setup for Jarvis on Linux Mint / Ubuntu.  Run:  ./install.sh
# Safe to run again (e.g. if something failed halfway).
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

say()  { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m    ✔ %s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m    ! %s\033[0m\n" "$*"; }
ask()  { read -r -p "    $1 [Y/n] " a; [[ -z "$a" || "$a" =~ ^[Yy] ]]; }

say "Step 1 of 6: Installing system packages (your password may be asked for)"
sudo apt-get update -q
sudo apt-get install -y -q python3 python3-venv python3-dev python3-pip libportaudio2 libsndfile1 espeak-ng git curl \
  mpv playerctl xdotool iproute2 iputils-ping gvfs-backends libglib2.0-bin
ok "System packages installed"

say "Step 2 of 6: Checking Python"
PY=python3
VER=$($PY -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
MINOR=$($PY -c 'import sys; print(sys.version_info[1])')
if [ "$MINOR" -lt 10 ]; then
  warn "Python $VER is too old. Jarvis needs Python 3.10 or newer (Linux Mint 21 or 22)."
  exit 1
fi
ok "Python $VER"

say "Step 3 of 6: Creating Jarvis's private Python folder (.venv) and installing libraries"
echo "    This downloads a few gigabytes the first time. It can take 5-15 minutes."
[ -d .venv ] || $PY -m venv .venv
.venv/bin/pip install -q --upgrade pip wheel
.venv/bin/pip install -q -r requirements.txt
# openwakeword lists an old dependency that doesn't install on new Python; we use its ONNX mode instead.
.venv/bin/pip install -q --no-deps "openwakeword>=0.6"
ok "Core libraries installed"

if ask "Install the built-in Kokoro voice (about 2-3 GB)? Say n if you run Kokoro in Docker."; then
  if .venv/bin/pip install -q "kokoro>=0.9"; then
    ok "Kokoro voice installed"
  else
    warn "Kokoro didn't install. Jarvis will still work: pick another voice engine on the settings page."
  fi
fi

if command -v nvidia-smi >/dev/null 2>&1; then
  echo "    NVIDIA graphics card found -- installing the libraries Whisper needs to use it."
  .venv/bin/python -c "import nvidia.cublas, nvidia.cudnn" 2>/dev/null || \
    .venv/bin/pip install -q nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
  ok "Graphics card support ready"
else
  warn "No NVIDIA driver found. Speech recognition will use the processor (slower)."
  warn "Tip: on the settings page choose Speech to text > Whisper model = small.en"
fi

if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  sudo ufw allow 4002/udp comment "Govee lights reply to Jarvis" >/dev/null && ok "Firewall: allowed Govee light replies"
fi

say "Step 4 of 6: Downloading wake word models and the offline dictionary"
.venv/bin/python -c "from openwakeword.utils import download_models; download_models()" >/dev/null 2>&1 \
  && ok "Wake word models ready" || warn "Couldn't download wake word models now; Jarvis will retry when it starts."
.venv/bin/python -c "from jarvis.tools.dictionary import ensure_wordnet; import sys; sys.exit(0 if ensure_wordnet() else 1)" >/dev/null 2>&1 \
  && ok "Offline dictionary ready" || warn "Couldn't download the dictionary now; Jarvis will retry when it starts."

say "Step 5 of 6: Checking the AI model (Ollama)"
if command -v ollama >/dev/null 2>&1; then
  ok "Ollama is installed"
  if ! ollama list 2>/dev/null | grep -q "qwen2.5:7b"; then
    if ask "Download the recommended starter model qwen2.5:7b (about 4.7 GB)?"; then
      ollama pull qwen2.5:7b && ok "Model downloaded"
    else
      warn "OK. Pick your own model later on the settings page (AI model > Load models)."
    fi
  else
    ok "qwen2.5:7b is already downloaded"
  fi
else
  warn "Ollama isn't installed. Install it with:  curl -fsSL https://ollama.com/install.sh | sh"
fi

say "Step 6 of 6: Setting up the background service and menu shortcut"
chmod +x run.sh manage.sh
mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/share/applications"
cat > "$HOME/.config/systemd/user/jarvis-bot.service" <<EOF
[Unit]
Description=Jarvis-Bot voice assistant
After=default.target sound.target pipewire.service

[Service]
ExecStart=$DIR/run.sh
WorkingDirectory=$DIR
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
cat > "$HOME/.local/share/applications/jarvis-bot.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Jarvis-Bot
Comment=Open Jarvis settings and chat
Exec=$DIR/run.sh --open
Icon=audio-input-microphone
Terminal=false
Categories=Utility;
EOF
systemctl --user daemon-reload
ok "Added 'Jarvis' to your Mint menu (it opens the settings page)"

if ask "Start Jarvis automatically every time you log in?"; then
  systemctl --user enable jarvis-bot >/dev/null 2>&1
  ok "Autostart is on"
fi
systemctl --user restart jarvis-bot
ok "Jarvis is starting in the background"

printf "\n\033[1;32mAll done!\033[0m\n"
echo "  • Open the settings page:  click 'Jarvis' in your Mint menu, or go to http://localhost:8765"
echo "  • The first start loads speech models (a few minutes). Watch the 'Activity' tab."
echo "  • Then say: \"Hey Jarvis\" ... \"what time is it?\""
echo "  • Controls:  ./manage.sh start | stop | restart | status | logs | update"
