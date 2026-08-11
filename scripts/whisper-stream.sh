#!/bin/bash
# whisper-stream.sh - Real-time audio transcription using whisper-cli
# Captures audio from the specified device, runs whisper in streaming mode,
# and outputs transcribed text to stdout (piped to dispatcher.py).
#
# Usage: ./whisper-stream.sh [device_name]
# Default device: "Meeting Capture" (BlackHole aggregate)

set -e

# Config
DEVICE="${1:-Meeting Capture}"
MODEL="${WHISPER_MODEL_RT:-$(brew --prefix)/share/whisper-cpp/models/ggml-large-v3-turbo.bin}"
LANGUAGE="${WHISPER_LANG:-en}"

# Segment settings (in seconds)
SEGMENT_LENGTH=5  # Record in 5-second chunks
SILENCE_THRESHOLD=0.01

# Temp directory for audio chunks
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

echo "[whisper-stream] Device: $DEVICE" >&2
echo "[whisper-stream] Model: $(basename $MODEL)" >&2
echo "[whisper-stream] Language: $LANGUAGE" >&2
echo "[whisper-stream] Streaming..." >&2

# Continuous capture and transcribe loop
SEGMENT=0
while true; do
    SEGMENT=$((SEGMENT + 1))
    WAVFILE="$TMPDIR/chunk-${SEGMENT}.wav"

    # Record a segment
    ffmpeg -f avfoundation -i ":${DEVICE}" \
        -ar 16000 -ac 1 -acodec pcm_s16le \
        -t "$SEGMENT_LENGTH" "$WAVFILE" \
        -loglevel quiet -y </dev/null 2>/dev/null

    # Check if file has audio (not just silence)
    VOLUME=$(ffmpeg -i "$WAVFILE" -af "volumedetect" -f null /dev/null 2>&1 | grep mean_volume | awk '{print $5}')
    if [ -n "$VOLUME" ]; then
        # Convert to absolute value for comparison
        ABS_VOLUME=$(echo "$VOLUME" | tr -d '-')
        # Skip if too quiet (silence threshold ~= -40dB)
        if [ "$(echo "$ABS_VOLUME < 40" | bc)" -eq 1 ]; then
            rm -f "$WAVFILE"
            continue
        fi
    fi

    # Transcribe
    RESULT=$(whisper-cli -m "$MODEL" -l "$LANGUAGE" --no-timestamps -f "$WAVFILE" 2>/dev/null | \
        grep -v "^\[" | grep -v "^$" | sed 's/^ *//')

    # Output non-empty results
    if [ -n "$RESULT" ] && [ "$RESULT" != "[BLANK_AUDIO]" ]; then
        echo "$RESULT"
    fi

    # Cleanup
    rm -f "$WAVFILE"
done
