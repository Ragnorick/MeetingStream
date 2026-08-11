#!/bin/bash
# meeting.sh - Main entry point for a meeting session
# Starts whisper streaming + dispatcher + background recording
#
# Usage: ./meeting.sh [--provider claude|openai|ollama]
# Stop: Ctrl+C

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="$PROJECT_DIR/config/session.toml"

# Parse args
PROVIDER_OVERRIDE=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --provider) PROVIDER_OVERRIDE="$2"; shift 2 ;;
        --config) CONFIG="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Session setup
DATE_FOLDER=$(date '+%Y-%m-%d')
SESSION_FOLDER="$DATE_FOLDER/$(date '+%H-%M')"
OUTPUT_DIR="$HOME/Documents/MeetingStream/$SESSION_FOLDER"
RECORDING_DIR="$HOME/meeting-notes/$DATE_FOLDER"
RECORDING_DEVICE="${RECORDING_DEVICE:-Meeting Capture}"
WAV_FILE="$RECORDING_DIR/recording-$(date '+%H-%M')-en.wav"

mkdir -p "$OUTPUT_DIR"
mkdir -p "$RECORDING_DIR"

# Start background recording
ffmpeg -f avfoundation -i ":${RECORDING_DEVICE}" \
    -ar 16000 -ac 1 -acodec pcm_s16le "$WAV_FILE" \
    -loglevel quiet </dev/null >/dev/null 2>&1 &
FFMPEG_PID=$!

echo "=== MeetingStream ==="
echo "  Recording: $WAV_FILE (PID: $FFMPEG_PID)"
echo "  Output:    $OUTPUT_DIR/"
echo ""

# Cleanup on exit
cleanup() {
    kill $FFMPEG_PID 2>/dev/null
    wait $FFMPEG_PID 2>/dev/null
    echo ""
    echo "Recording saved: $WAV_FILE"
    echo "Notes saved:     $OUTPUT_DIR/"
    echo "Transcribe:      transcribe $WAV_FILE en"
}
trap cleanup EXIT INT TERM

# Start whisper streaming piped to dispatcher
"$SCRIPT_DIR/whisper-stream.sh" "$RECORDING_DEVICE" | \
    python3 "$SCRIPT_DIR/dispatcher.py" --config "$CONFIG" --output "$OUTPUT_DIR"
