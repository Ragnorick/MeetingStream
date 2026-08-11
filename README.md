# MeetingStream

Live AI-powered meeting transcription and notes on macOS. Fully local audio processing with cloud LLM for intelligent note-taking.

**What you get:** Real-time meeting notes, verbatim transcript, background WAV recording, offline transcription. All audio stays on your machine.

## How It Works

```
Meeting App (Zoom/Teams/Meet)
    → Multi-Output Device (Audio MIDI Setup)
        → Your headphones (you hear normally)
        → BlackHole 2ch (virtual loopback)

BlackHole 2ch + Microphone
    → Aggregate Device "Meeting Capture"
        → whisper-stream.sh (real-time Whisper large-v3-turbo)
            → dispatcher.py (routes to AI agents)
                → transcript.md (verbatim, append-only)
                → meeting-notes.md (structured, auto-updating)
        → ffmpeg background (16kHz mono WAV recording)

Post-meeting:
    recording.wav → whisper-cli (medium model) → timestamped .md
```

## Quick Start

### Prerequisites

```bash
brew install blackhole-2ch    # Virtual audio driver (restart Mac after)
brew install whisper-cpp       # Offline transcription (installs as whisper-cli)
brew install ffmpeg            # Audio recording
pip install -r requirements.txt
```

### Download Whisper Models

```bash
# Real-time (streaming during meetings)
mkdir -p ~/.cache/talkstream/models
curl -L "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin" \
    -o ~/.cache/talkstream/models/ggml-large-v3-turbo.bin

# Offline (post-meeting transcription)
WHISPER_MODELS="$(brew --prefix)/share/whisper-cpp/models"
mkdir -p "$WHISPER_MODELS"
curl -L "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin" \
    -o "$WHISPER_MODELS/ggml-medium.bin"
```

### Audio Setup (one-time, in Audio MIDI Setup)

1. **Multi-Output Device**: Your headphones + BlackHole 2ch (drift correction ON for BlackHole)
2. **Aggregate Device "Meeting Capture"**: BlackHole 2ch + Built-in Microphone (drift correction ON for mic)
3. **In Zoom/Teams**: Set speaker output to "Multi-Output Device"

### API Key

```bash
# Claude (recommended)
export ANTHROPIC_API_KEY="sk-ant-..."

# Or OpenAI
export OPENAI_API_KEY="sk-..."

# Add to ~/.zshrc to persist
```

### Run

```bash
# Make scripts executable (once)
chmod +x scripts/*.sh

# Start a meeting
./scripts/meeting.sh

# Ctrl+C to end
```

### Shell Integration (optional)

Append `config/zshrc-block.sh` to your `~/.zshrc` for quick aliases:

```bash
cat config/zshrc-block.sh >> ~/.zshrc
source ~/.zshrc

# Then just:
meeting              # start a meeting session
transcribe last      # offline transcribe most recent recording
```

## Configuration

Edit `config/session.toml`:

```toml
[llm]
provider = "claude"       # or "openai" or "ollama"
# model = "claude-sonnet-4-20250514"

[audio]
device = "Meeting Capture"
language = "en"

agents = ["transcriber", "note-taker"]
```

## Agent Prompts

Agent behavior is defined in markdown files in `agents/`:

- `transcriber.md` — Verbatim append-only transcript
- `note-taker.md` — Structured meeting notes (rewrites each turn)
- `sketch-artist.md` — Mermaid architecture diagrams (on-demand)

Edit these to customize agent behavior.

## Output

```
~/Documents/MeetingStream/
└── 2026-08-10/
    └── 14-30/
        ├── transcript.md
        └── meeting-notes.md

~/meeting-notes/
└── 2026-08-10/
    ├── recording-14-30-en.wav
    └── recording-14-30-en.md  (after running: transcribe last)
```

## Cost

- **Audio capture + transcription**: Free (all local)
- **AI note-taking (Claude)**: ~$0.10-0.50 per meeting depending on length
- **AI note-taking (Ollama)**: Free (local, lower quality)

## Future: Product Tiers

- **Free tier**: Ollama (local LLM) — lower quality but zero cost
- **Pro tier**: Bring your own API key (Claude/OpenAI) — user pays their own usage
- **Managed tier**: Hosted LLM with hourly pricing — simplest for end users

## License

MIT
