#!/bin/bash
# file-meeting.sh - Classify and file meeting notes after a TalkStream session
# Called automatically on Ctrl+C from the talk-remote-en wrapper, or manually.
#
# Usage: ./file-meeting.sh <session_output_dir> [session_start_time_ISO]
#   e.g.: ./file-meeting.sh ~/Documents/TalkStream/2026-08-10/13-00 2026-08-10T13:00:00
#
# What it does:
#   1. Looks up the calendar event that was happening at session start time
#   2. Classifies by project using routing.toml keywords
#   3. Copies meeting-notes.md to the appropriate OneDrive project folder
#   4. Renames per convention: YYYY-MM-DD - [Tag] Calendar Title.md

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="$(dirname "$SCRIPT_DIR")/config"
ROUTING_CONFIG="$CONFIG_DIR/routing.toml"

SESSION_DIR="${1:?Usage: file-meeting.sh <session_output_dir> [start_time_ISO]}"
SESSION_START="${2:-}"

# --- Validate ---
NOTES_FILE="$SESSION_DIR/meeting-notes.md"
if [ ! -f "$NOTES_FILE" ]; then
    echo "[file-meeting] No meeting-notes.md found in $SESSION_DIR — skipping filing."
    exit 0
fi

# --- Determine session start time ---
if [ -z "$SESSION_START" ]; then
    # Infer from directory name (format: ~/Documents/TalkStream/YYYY-MM-DD/HH-MM/)
    DATE_PART=$(basename "$(dirname "$SESSION_DIR")")  # 2026-08-10
    TIME_PART=$(basename "$SESSION_DIR")               # 13-00
    HOUR="${TIME_PART%-*}"
    MINUTE="${TIME_PART#*-}"
    SESSION_START="${DATE_PART}T${HOUR}:${MINUTE}:00"
fi

MEETING_DATE="${SESSION_START%%T*}"  # 2026-08-10

echo "[file-meeting] Session: $SESSION_DIR"
echo "[file-meeting] Looking up calendar for: $SESSION_START"

# --- Query calendar ---
# Find meetings happening at session start time
# Use mcscli to query meetings.amazon.com (handles Midway auth)
MEETING_DATE="${SESSION_START%%T*}"
CAL_JSON=$(mcscli curl -s "https://meetings.amazon.com/calendar/find/rickvan?startTime=${MEETING_DATE}T00:00:00Z&endTime=${MEETING_DATE}T23:59:59Z" 2>/dev/null || echo "{}")

# Use python to find the matching meeting
SESSION_HOUR=$(echo "$SESSION_START" | cut -dT -f2 | cut -d: -f1)
SESSION_MIN=$(echo "$SESSION_START" | cut -dT -f2 | cut -d: -f2)

CALENDAR_TITLE=$(python3 << PYTHON_EOF
import json
import sys
from datetime import datetime, timezone, timedelta

cal_data = '''$CAL_JSON'''
session_hour = int("$SESSION_HOUR")
session_min = int("$SESSION_MIN")

try:
    meetings = json.loads(cal_data).get("meetings", [])
except:
    print("")
    sys.exit(0)

# Filter to real meetings (not OOO, not all-day, not private blockers, not canceled)
real_meetings = []
for m in meetings:
    subj = m.get("subject", "")
    status = m.get("status", "")
    start_time = m.get("time", {}).get("startTime", "")
    end_time = m.get("time", {}).get("endTime", "")

    if any(x in subj for x in ["OOO", "OOTO", "ooo", "Canceled:"]):
        continue
    if m.get("isPrivate") and "Block" in subj:
        continue
    if status in ("free", "outOfOffice") and ("Block" in subj or "Birthday" in subj):
        continue

    try:
        mt_start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        mt_end = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
        duration_hours = (mt_end - mt_start).total_seconds() / 3600
        if duration_hours > 12:
            continue
    except:
        continue

    real_meetings.append(m)

# Find meeting closest to session start (within 60 min)
best_match = None
best_diff = float('inf')

for m in real_meetings:
    start_time = m.get("time", {}).get("startTime", "")
    try:
        parts = start_time.split("T")[1].split(":")
        mt_hour = int(parts[0])
        mt_min = int(parts[1])
        # Convert UTC to ET (approximate: UTC-4 for EDT)
        mt_hour_local = (mt_hour - 4) % 24
        diff = abs(mt_hour_local - session_hour) * 60 + abs(mt_min - session_min)
        if diff < best_diff:
            best_diff = diff
            best_match = m
    except:
        continue

if best_match and best_diff <= 60:
    print(best_match.get("subject", ""))
else:
    print("")
PYTHON_EOF
)

# --- If no calendar title found, use first line of meeting notes or ask ---
if [ -z "$CALENDAR_TITLE" ]; then
    # Try to extract from meeting-notes.md header
    CALENDAR_TITLE=$(head -5 "$NOTES_FILE" | grep "^# " | head -1 | sed 's/^# //' | sed 's/ — .*//')
    if [ -z "$CALENDAR_TITLE" ] || [ "$CALENDAR_TITLE" = "Meeting Notes" ]; then
        echo "[file-meeting] Could not determine meeting title from calendar or notes."
        echo "[file-meeting] Enter meeting title (or press Enter to skip filing):"
        read -r CALENDAR_TITLE
        if [ -z "$CALENDAR_TITLE" ]; then
            echo "[file-meeting] Skipped."
            exit 0
        fi
    fi
fi

echo "[file-meeting] Title: $CALENDAR_TITLE"

# --- Classify ---
# Use python to match against routing.toml
CLASSIFICATION=$(python3 << PYTHON_EOF
import sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib

config_path = "$ROUTING_CONFIG"
title = """$CALENDAR_TITLE"""

with open(config_path, "rb") as f:
    config = tomllib.load(f)

base_path = config["routing"]["base_path"]
unclassified = config["routing"]["unclassified_path"]

# Check known series first (exact match)
for series in config.get("known_series", []):
    if series["pattern"].lower() in title.lower():
        project_name = series["project"]
        # Find the project config
        for p in config.get("project", []):
            if p["name"] == project_name:
                if p.get("folder") == "_ABSOLUTE_":
                    folder = p.get("absolute_path", unclassified)
                else:
                    folder = f"{base_path}/{p['folder']}"
                sub = series.get("sub_route")
                if sub:
                    for sr in p.get("sub_route", []):
                        if sr["name"] == sub:
                            print(f"{folder}/{sr['subfolder']}|{project_name}")
                            sys.exit(0)
                notes_sub = p.get("notes_subfolder", "")
                if notes_sub:
                    print(f"{folder}/{notes_sub}|{project_name}")
                else:
                    print(f"{folder}|{project_name}")
                sys.exit(0)

# Keyword matching
for project in config.get("project", []):
    for keyword in project.get("keywords", []):
        if keyword.lower() in title.lower():
            # Determine folder path
            if project.get("folder") == "_ABSOLUTE_":
                folder = project.get("absolute_path", unclassified)
            else:
                folder = f"{base_path}/{project['folder']}"
            # Check sub-routes
            for sr in project.get("sub_route", []):
                for sr_kw in sr.get("keywords", []):
                    if sr_kw.lower() in title.lower():
                        print(f"{folder}/{sr['subfolder']}|{project['name']}")
                        sys.exit(0)
            notes_sub = project.get("notes_subfolder", "")
            if notes_sub:
                print(f"{folder}/{notes_sub}|{project['name']}")
            else:
                print(f"{folder}|{project['name']}")
            sys.exit(0)

# Unclassified
print(f"{unclassified}|Unclassified")
PYTHON_EOF
)

DEST_FOLDER=$(echo "$CLASSIFICATION" | cut -d'|' -f1)
TAG=$(echo "$CLASSIFICATION" | cut -d'|' -f2)

echo "[file-meeting] Classification: [$TAG]"
echo "[file-meeting] Destination: $DEST_FOLDER"

# --- Generate filename ---
# Strip common prefixes that are now in the tag
CLEAN_TITLE="$CALENDAR_TITLE"
# Remove "OFA Exit", "OFA-AP exit", "Exit-OFA" prefixes since they're in the tag
CLEAN_TITLE=$(echo "$CLEAN_TITLE" | sed -E 's/^(OFA[- ]?(AP)?[- ]?[Ee]xit|Exit[- ]?OFA)[^a-zA-Z]*//')
# Remove "please prioritize" and similar preambles
CLEAN_TITLE=$(echo "$CLEAN_TITLE" | sed -E 's/^please prioritize[[:space:]]*\[?[^\]]*\]?[[:space:]]*//')
# Sanitize for filename (remove : ? * " < > |)
CLEAN_TITLE=$(echo "$CLEAN_TITLE" | tr ':?*"<>|' '       ' | sed 's/  */ /g' | sed 's/^ *//;s/ *$//')
# Trim length
CLEAN_TITLE=$(echo "$CLEAN_TITLE" | cut -c1-80)

FILENAME="${MEETING_DATE} - [${TAG}] ${CLEAN_TITLE}.md"

# Check for duplicate — only add time if same filename exists
if [ -f "$DEST_FOLDER/$FILENAME" ]; then
    TIME_PART=$(basename "$SESSION_DIR")  # HH-MM
    FILENAME="${MEETING_DATE} ${TIME_PART} - [${TAG}] ${CLEAN_TITLE}.md"
fi

# --- Create destination and copy ---
mkdir -p "$DEST_FOLDER"
cp "$NOTES_FILE" "$DEST_FOLDER/$FILENAME"

echo "[file-meeting] Filed: $FILENAME"
echo "[file-meeting] → $DEST_FOLDER/"

# Add a reference line to the filed copy pointing back to full session
echo "" >> "$DEST_FOLDER/$FILENAME"
echo "---" >> "$DEST_FOLDER/$FILENAME"
echo "> Full session: \`$SESSION_DIR/\`" >> "$DEST_FOLDER/$FILENAME"
