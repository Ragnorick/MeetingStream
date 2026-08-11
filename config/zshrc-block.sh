# --- MeetingStream Config (Personal) ---
# Add this block to ~/.zshrc on your personal Mac

WHISPER_MODEL="$(brew --prefix)/share/whisper-cpp/models/ggml-medium.bin"
WHISPER_MODEL_RT="$(brew --prefix)/share/whisper-cpp/models/ggml-large-v3-turbo.bin"
RECORDING_DEVICE="Meeting Capture"
MEETINGSTREAM_DIR="$HOME/Projects/MeetingStream"  # adjust to wherever you clone the repo

# --- Start a meeting ---
alias meeting='$MEETINGSTREAM_DIR/scripts/meeting.sh'

# --- Offline transcription ---
transcribe() {
    local input_file="$1" lang_override="$2"
    [ -z "$input_file" ] && { echo "Usage: transcribe <file|last> [lang]"; return 1; }
    [ "$input_file" = "last" ] && input_file=$(find ~/meeting-notes -name "recording-*.wav" | sort | tail -1)
    [ ! -f "$input_file" ] && { echo "Not found: $input_file"; return 1; }

    local lang="$lang_override"
    [ -z "$lang" ] && case "$input_file" in *-pt.wav) lang="pt";; *-en.wav) lang="en";; esac

    local base="${input_file%.wav}" lang_args=()
    [ -n "$lang" ] && lang_args=(--language "$lang")

    whisper-cli -m "$WHISPER_MODEL" -osrt -of "$base" "${lang_args[@]}" -f "$input_file"

    # Convert SRT to timestamped Markdown
    awk '/^[0-9]+$/{next} /-->/{split($1,t,",");ts=t[1];next} /^$/{next} {printf "[%s] %s\n\n",ts,$0}' \
        "${base}.srt" > "${base}.md"
    rm -f "${base}.srt"
    echo "Transcript: ${base}.md ($(wc -w < "${base}.md") words)"
}
