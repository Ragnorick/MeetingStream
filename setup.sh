#!/bin/bash
# MeetingStream - One-time setup script for macOS
# Run this on your personal Mac after cloning the repo.
#
# Usage:
#   git clone git@github.com:Ragnorick/meetingstream.git
#   cd meetingstream
#   ./setup.sh

set -e
echo "=== MeetingStream Setup ==="
echo ""

# --- 1. Install Homebrew dependencies ---
echo "[1/5] Installing brew packages..."
brew install blackhole-2ch 2>/dev/null || echo "  blackhole-2ch already installed"
brew install whisper-cpp 2>/dev/null || echo "  whisper-cpp already installed"
brew install ffmpeg 2>/dev/null || echo "  ffmpeg already installed"
echo "  Done. NOTE: Restart your Mac after first blackhole-2ch install."
echo ""

# --- 2. Download Whisper models ---
echo "[2/5] Downloading Whisper models (~3GB total)..."
# Real-time model (for streaming during meetings)
mkdir -p ~/.cache/talkstream/models
if [ ! -f ~/.cache/talkstream/models/ggml-large-v3-turbo.bin ]; then
    echo "  Downloading large-v3-turbo (1.5GB)..."
    curl -L "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin" \
        -o ~/.cache/talkstream/models/ggml-large-v3-turbo.bin
else
    echo "  large-v3-turbo already downloaded"
fi

# Offline transcription model
WHISPER_MODELS="$(brew --prefix)/share/whisper-cpp/models"
mkdir -p "$WHISPER_MODELS"
if [ ! -f "$WHISPER_MODELS/ggml-medium.bin" ]; then
    echo "  Downloading medium (1.5GB)..."
    curl -L "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin" \
        -o "$WHISPER_MODELS/ggml-medium.bin"
else
    echo "  medium already downloaded"
fi
echo "  Done."
echo ""

# --- 3. Install Python dependencies ---
echo "[3/5] Installing Python packages..."
pip3 install -r requirements.txt
echo "  Done."
echo ""

# --- 4. Make scripts executable ---
echo "[4/5] Setting permissions..."
chmod +x scripts/*.sh
echo "  Done."
echo ""

# --- 5. API Key check ---
echo "[5/5] Checking API key..."
if [ -n "$ANTHROPIC_API_KEY" ]; then
    echo "  ANTHROPIC_API_KEY is set."
elif [ -n "$OPENAI_API_KEY" ]; then
    echo "  OPENAI_API_KEY is set."
else
    echo ""
    echo "  WARNING: No API key found!"
    echo "  Set one of these in your ~/.zshrc:"
    echo ""
    echo "    export ANTHROPIC_API_KEY=\"sk-ant-...\"    # Get from console.anthropic.com"
    echo "    export OPENAI_API_KEY=\"sk-...\"           # Get from platform.openai.com"
    echo ""
    echo "  Or use Ollama (free, local) by setting provider = \"ollama\" in config/session.toml"
    echo "  Install Ollama: brew install ollama && ollama pull llama3.1:8b"
fi
echo ""

# --- Done ---
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Restart Mac (if first time installing BlackHole)"
echo "  2. Set up Audio MIDI devices (see README.md)"
echo "  3. Set your API key in ~/.zshrc"
echo "  4. Run: ./scripts/meeting.sh"
echo ""
echo "Optional: Add shell aliases to ~/.zshrc:"
echo "  cat config/zshrc-block.sh >> ~/.zshrc && source ~/.zshrc"
echo "  Then just type: meeting"
