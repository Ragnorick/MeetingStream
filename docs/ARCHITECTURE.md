# MeetingStream — Architecture & Feature Documentation

This document tracks all features, decision logic, and architecture choices for MeetingStream.
Use it for: understanding how things work, translating to customers, marketing, and market-fit evaluation.

---

## System Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                        MeetingStream                             │
│                                                                  │
│  ┌─────────────┐    ┌─────────────┐    ┌──────────────────────┐ │
│  │   Audio     │    │  Real-time  │    │    AI Agents         │ │
│  │   Capture   │───▶│    STT      │───▶│  (parallel dispatch) │ │
│  │             │    │  (Whisper)  │    │                      │ │
│  └─────────────┘    └─────────────┘    └──────────┬───────────┘ │
│        │                                          │             │
│        ▼                                          ▼             │
│  ┌─────────────┐                         ┌──────────────────┐  │
│  │  Recording  │                         │  Output Files    │  │
│  │  (ffmpeg)   │                         │  (.md live docs) │  │
│  └─────────────┘                         └────────┬─────────┘  │
│                                                   │             │
│                                          ┌────────▼─────────┐  │
│                                          │  Meeting Router  │  │
│                                          │  (classify+file) │  │
│                                          └──────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

---

## Core Pipeline

### 1. Audio Capture Layer

| Component | What it does | Technology |
|-----------|-------------|------------|
| BlackHole 2ch | Virtual audio loopback — routes meeting audio to capture | Open source (brew) |
| Multi-Output Device | Splits audio: you hear + BlackHole captures | macOS Audio MIDI Setup |
| Meeting Capture (Aggregate) | Combines BlackHole (remote) + mic (you) | macOS Audio MIDI Setup |
| ffmpeg | Background 16kHz mono WAV recording | Open source (brew) |

**User experience:** Zero interaction. Audio routing is configured once. User just joins meetings normally.

### 2. Speech-to-Text Layer

| Component | What it does | Technology |
|-----------|-------------|------------|
| Whisper large-v3-turbo | Real-time transcription during meetings | whisper.cpp (open source) |
| Whisper medium | Post-meeting offline transcription | whisper.cpp (open source) |
| Voice Activity Detection | Detects speech vs silence to chunk audio | Silero VAD |

**Key specs:**
- Real-time: ~2x faster than real-time on Apple Silicon (M1-M5)
- Fully local: no audio leaves the machine
- Language: configurable (English default, supports 99 languages)

### 3. AI Agent Dispatch Layer

Three agents run in parallel during meetings. Each receives transcribed text chunks and maintains its own output file.

| Agent | Output File | Behavior | Update Pattern |
|-------|-------------|----------|----------------|
| **Transcriber** | `transcript.md` | Verbatim record with timestamps and speaker labels | Append-only |
| **Note-taker** | `meeting-notes.md` | Structured summary (decisions, action items, discussion) | Full-rewrite each turn |
| **Sketch-artist** | `sketch.md` | Mermaid architecture diagrams | Full-rewrite (only on arch discussion) |

**Dispatch logic:**
- Transcriber gets EVERYTHING (every utterance)
- Note-taker gets substantive content (>5 words, not filler)
- Sketch-artist only fires when architecture keywords are detected

**Agent prompts** are plain markdown files in `agents/` — fully customizable by the user.

### 4. Meeting Router (Post-Meeting Classification)

Runs automatically when meeting ends. Classifies the meeting and copies notes to the appropriate project folder.

---

## Meeting Router — Deep Dive

### Modes

| Mode | How it classifies | Cost per meeting | Best for |
|------|------------------|-----------------|----------|
| **deterministic** | Keyword matching against `routing.toml` | Free | Power users with established folder structure |
| **agentic** | LLM reads notes + folder list → picks destination | ~$0.02 | New users, complex/ambiguous meetings |
| **hybrid** (recommended) | Tries keywords first → falls back to LLM | Free usually, $0.02 occasionally | Everyone |

### Hybrid Mode — Step-by-Step Flow

```
Meeting ends (Ctrl+C or Stop button)
    │
    ▼
┌─ Step 1: Check learned rules ──────────────────────────────────┐
│  Source: ~/.config/meetingstream/learned-routes.json            │
│  Logic: Does this title match a pattern from a previous filing?│
│  Cost: Free (instant local file read)                          │
│  Result: If match → use that destination → DONE                │
└────────────────────────────────────────────────────────────────┘
    │ (no match)
    ▼
┌─ Step 2: Deterministic keyword matching ───────────────────────┐
│  Source: config/routing.toml (user-configured keywords)        │
│  Logic: Does calendar title contain any project keyword?       │
│  Cost: Free (instant string matching)                          │
│  Result: If match → file to configured folder → DONE           │
└────────────────────────────────────────────────────────────────┘
    │ (no match)
    ▼
┌─ Step 3: Agentic (LLM) classification ────────────────────────┐
│  What happens:                                                 │
│    a) Scan user's folder structure (quick directory listing)   │
│    b) Read first 2000 chars of meeting-notes.md                │
│    c) Send to LLM: "Here's the meeting content + folders,     │
│       where does this belong?"                                 │
│    d) LLM responds with: folder path, tag, confidence score   │
│  Cost: ~$0.01-0.03 (one LLM call, small prompt)               │
│  Result: If confidence >= 0.7 → file → learn rule → DONE      │
└────────────────────────────────────────────────────────────────┘
    │ (confidence < 0.7)
    ▼
┌─ Step 4: Ask the user ────────────────────────────────────────┐
│  Prompt: "Low confidence. I'd file this to [X]. OK? [Y/n]"   │
│  User confirms or provides custom path                         │
│  Result: File → learn rule → DONE                              │
└────────────────────────────────────────────────────────────────┘
```

### Learned Rules — How They Build

Learned rules are the **self-improving** part of the system. They start empty and build from usage:

| Event | What happens to learned rules |
|-------|-------------------------------|
| First install | `learned-routes.json` doesn't exist (empty) |
| Meeting #1 (new topic) | LLM classifies → files → saves rule: `{"pattern": "weekly anu", "destination": "/path/...", "tag": "1:1"}` |
| Meeting #2 (different topic) | LLM classifies → files → saves another rule |
| Meeting #3 (same as #1) | Learned rule matches → instant, free, no LLM call |
| After ~10 meetings | Most recurring meetings auto-route via learned rules |

**Rules are only created when:**
- The agentic path (Step 3) fires AND successfully files a meeting
- The user confirms a low-confidence filing (Step 4)

**Rules are NOT created by:**
- Folder scanning (scan is read-only context for the LLM)
- Deterministic keyword matching (those are manual in routing.toml)
- Simply running the app with no meetings

### Learned Rules — Staleness & Invalidation

Learned rules store a hardcoded destination path. If the user renames, moves, or deletes that folder, the rule becomes stale.

**Current behavior (simple fix):**
- When a learned rule matches a title, the system checks if the destination path still exists on disk
- If the folder is gone → rule is automatically deleted from `learned-routes.json`
- The meeting falls through to Step 2/3 for fresh classification
- User sees: `[route] Learned rule invalidated: 'weekly anu' → folder no longer exists`

**Future behavior (UI build — Settings panel):**
- Show all learned rules in a list with edit/delete buttons
- Visual indicator (red/yellow) for rules pointing to missing or deleted folders
- Visual indicator (green + "new") for recently created folders that have no rules pointing to them
- "Re-evaluate all" button that scans the folder structure and flags discrepancies
- Ability to manually reassign a stale rule to a new folder path without waiting for the LLM

### Folder Scan — What It Does and Doesn't Do

| Does | Doesn't |
|------|---------|
| Lists all folders in the user's base_path (up to 3 levels deep) | Create or modify any rules |
| Provides that list as text context to the LLM prompt | Read file contents inside folders |
| Runs every time the agentic path (Step 3) fires | Run on its own schedule |
| Completes in milliseconds (just a directory listing) | Cache results between runs |

The LLM uses folder names semantically. If a folder is named "Anu Meeting Notes" and the meeting title is "Weekly 1:1 with Anu", the LLM connects them — no keyword config needed.

### Filename Convention

```
YYYY-MM-DD - [Tag] Meeting Title.md
```

- Date comes from the meeting session
- Tag comes from the classification (project name)
- Title comes from the calendar event
- Common prefixes stripped (e.g., "OFA Exit" removed since it's in the tag)
- Time (HH-MM) only appended if a duplicate filename already exists

Examples:
```
2026-08-10 - [Exit-OFA] Retail Functionality Testing.md
2026-08-10 - [Exit-OFA] C2FO Design Alignment - P0 FR Migration.md
2026-08-10 - [Anu 1:1] Weekly Sync.md
2026-08-11 - [Unclassified] Random Planning Session.md
```

---

## Product Architecture — Provider & Pricing Model

### LLM Provider Abstraction

One API interface, multiple backends. User configures once in `session.toml`:

```toml
[llm]
provider = "claude"  # or "openai", "ollama", "managed"
```

| Provider | Who pays | Config needed | Use case |
|----------|---------|---------------|----------|
| `claude` | User (own API key) | `ANTHROPIC_API_KEY` env var | BYOK Pro tier |
| `openai` | User (own API key) | `OPENAI_API_KEY` env var | BYOK Pro tier |
| `ollama` | Nobody (runs locally) | Just `ollama pull llama3.1:8b` | Free tier |
| `managed` | Us (billed to customer via subscription) | `MEETINGSTREAM_API_KEY` | Managed tier |

### Pricing Tiers (Future Product)

```
┌─────────────────────────────────────────────────────────┐
│                    MeetingStream Pricing                 │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  FREE              PRO (BYOK)          MANAGED          │
│  ────              ──────────          ───────          │
│  Ollama local      Bring own key       We handle it    │
│  Lower quality     Full quality        Full quality    │
│  $0/month          $X/month (lower)    $Y/month        │
│                                                         │
│  Features:         Everything in       Everything in   │
│  - Recording       Free, plus:         Pro, plus:      │
│  - Transcription   - Claude/GPT        - No API key    │
│  - Basic notes       agents              needed        │
│  - File routing    - Agentic routing   - Usage metered │
│    (learned only)  - Full hybrid mode  - Priority      │
│                    - Sketch artist       support       │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### Cost Analysis (for pricing the Managed tier)

| Operation | Cost per meeting | Frequency |
|-----------|-----------------|-----------|
| Transcriber agent | ~$0.03-0.10 | Every meeting |
| Note-taker agent | ~$0.05-0.20 | Every meeting |
| Sketch-artist agent | ~$0.02-0.05 | ~20% of meetings |
| Meeting router | ~$0.01-0.03 | First few meetings, then free (learned) |
| **Total per meeting** | **~$0.10-0.40** | |
| **Monthly (20 meetings)** | **~$2-8 in LLM costs** | |

Pricing the managed tier at $15-25/month would cover costs + margin for a typical user.

---

## Output Structure

### During a meeting (live files)

```
~/Documents/MeetingStream/YYYY-MM-DD/HH-MM/
├── transcript.md        ← verbatim, append-only, timestamped
├── meeting-notes.md     ← structured summary, rewrites each turn
└── sketch.md            ← mermaid diagrams (if architecture discussed)
```

### After filing (copied to project folder)

```
~/Documents/Projects/01 Exit OFA/00 Meeting Notes/
└── 2026-08-10 - [Exit-OFA] Retail Functionality Testing.md
    (copy of meeting-notes.md, renamed, with session reference link at bottom)
```

### Recordings (always local, never filed to cloud)

```
~/meeting-notes/YYYY-MM-DD/
├── recording-14-30-en.wav    ← raw 16kHz mono audio
└── recording-14-30-en.md     ← offline whisper transcription (after `transcribe last`)
```

---

## Key Design Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Audio processing | Fully local (whisper.cpp) | Privacy — no meeting audio leaves the machine |
| Agent dispatch | Cloud LLM (Claude/OpenAI) | Quality — local models can't match for meeting notes |
| Recording format | 16kHz mono WAV | Standard for STT, ~115 MB/hour, lossless |
| Note-taker behavior | Full rewrite each turn | Produces best output — reassesses importance as meeting evolves |
| Transcriber behavior | Append-only | Preserves complete chronological record |
| Router mode | Hybrid (keywords → LLM fallback) | Free for recurring meetings, smart for new ones |
| Learned rules storage | Local JSON file | Simple, portable, no database needed |
| Folder scan | Every agentic run | Catches newly created folders without config changes |
| Filing | Copy (not move) | Original always preserved in session folder |
| Filename convention | Date-Tag-Title | Scannable in file browser, sortable by date |

---

## Future Enhancements (Not Yet Built)

| Feature | Description | Priority |
|---------|-------------|----------|
| macOS menu bar app | Swift/SwiftUI UI wrapping the terminal workflow | High — see CONTEXT-UI-BUILD.md |
| Speaker diarization | Identify who said what (multiple remote speakers) | Medium — hardware-limited |
| Calendar pre-fetch | Show upcoming meetings, one-click "Join + Record" | Medium |
| Bulk rule bootstrap | "Scan my past meetings and pre-populate learned rules" | Low |
| Meeting search | Full-text search across past transcripts | Low (MeshClaw/Kiro Crew handles this) |
| Team sharing | Shared routing configs, team folder structures | Future (product feature) |
| Mobile companion | View meeting notes on phone during/after meetings | Future |
