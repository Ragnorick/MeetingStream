#!/usr/bin/env python3
"""
Agentic Meeting Router
Classifies meeting notes and routes them to the appropriate folder.

Modes:
  - "agentic"       → LLM reads meeting notes + scans folders → picks destination
  - "deterministic" → keyword matching against routing.toml (no LLM call)
  - "hybrid"        → tries deterministic first, falls back to agentic if uncertain

The agentic mode:
  1. Scans user's folder structure (first run or on-demand)
  2. Reads the meeting notes content
  3. Asks the LLM: "where does this belong?"
  4. If confidence is low, asks the user (interactive)
  5. Learns from corrections → saves rules for next time

Usage:
  python3 route_meeting.py <session_dir> [--mode agentic|deterministic|hybrid]

Provider config in session.toml [llm] section — same provider used for
dispatch and routing (one API key, one bill).
"""

import argparse
import json
import os
import sys
import shutil
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib


# ---------------------------------------------------------------------------
# Provider layer (shared with dispatcher.py — same abstraction)
# ---------------------------------------------------------------------------

class LLMProvider:
    """Base class for LLM providers."""
    def chat(self, system_prompt: str, user_message: str) -> str:
        raise NotImplementedError


class ClaudeProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def chat(self, system_prompt: str, user_message: str) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "gpt-4o"):
        import openai
        self.client = openai.OpenAI(api_key=api_key)
        self.model = model

    def chat(self, system_prompt: str, user_message: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            max_tokens=1024,
        )
        return response.choices[0].message.content


class OllamaProvider(LLMProvider):
    def __init__(self, model: str = "llama3.1:8b", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url

    def chat(self, system_prompt: str, user_message: str) -> str:
        import urllib.request
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        return result["message"]["content"]


class ManagedProvider(LLMProvider):
    """Future: proxies through your SaaS backend for managed customers."""
    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url
        self.api_key = api_key

    def chat(self, system_prompt: str, user_message: str) -> str:
        import urllib.request
        payload = {
            "system": system_prompt,
            "message": user_message,
        }
        req = urllib.request.Request(
            f"{self.api_url}/chat",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        return result["response"]


class BedrockProvider(LLMProvider):
    """AWS Bedrock provider — uses Claude via Amazon's infrastructure.
    
    Authentication: Uses credentials exported by the claude CLI tool,
    or falls back to standard AWS credential chain (env vars, profiles).
    
    For Amazon employees: free via internal Bedrock access.
    For product customers: they'd need their own AWS account with Bedrock enabled.
    
    NOTE FOR PRODUCTIZATION: 
    - This provider works for internal/dev use (free via employer AWS access)
    - For BYOK customers with AWS accounts, they'd set credential_cmd=None
      and use AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars
    - For managed tier, replace with ManagedProvider (proxy through your backend)
    - Change the credential_cmd default to None before shipping publicly
    """
    def __init__(self, model="anthropic.claude-sonnet-4-20250514-v1:0",
                 region="us-west-2", credential_cmd=None):
        self.model = model
        self.region = region
        self.credential_cmd = credential_cmd

    def _get_credentials(self) -> dict:
        """Get AWS credentials via claude CLI export or env vars."""
        import subprocess

        # Try claude CLI credential export (Amazon internal), with retries
        if self.credential_cmd:
            for attempt in range(3):
                try:
                    result = subprocess.run(
                        self.credential_cmd.split(),
                        capture_output=True, text=True, timeout=30
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        # Parse JSON, skip any info lines
                        for line in result.stdout.strip().split("\n"):
                            line = line.strip()
                            if line.startswith("{"):
                                creds = json.loads(line)
                                c = creds.get("Credentials", creds)
                                return {
                                    "access_key": c["AccessKeyId"],
                                    "secret_key": c["SecretAccessKey"],
                                    "session_token": c.get("SessionToken", ""),
                                }
                except Exception as e:
                    if attempt == 2:
                        print(f"[bedrock] Credential export failed after 3 attempts: {e}", file=sys.stderr)

        # Fall back to environment variables
        access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        session_token = os.environ.get("AWS_SESSION_TOKEN", "")
        if access_key and secret_key:
            return {"access_key": access_key, "secret_key": secret_key, "session_token": session_token}

        print("Error: No AWS credentials available for Bedrock. Run mwinit or set AWS env vars.", file=sys.stderr)
        sys.exit(1)

    def chat(self, system_prompt: str, user_message: str) -> str:
        # Use boto3 (AWS SDK) — handles SigV4 signing and credentials reliably.
        # NOTE FOR PRODUCTIZATION: boto3 is a dependency for the bedrock provider.
        # BYOK/managed customers using claude/openai providers don't need it.
        import boto3

        creds = self._get_credentials()

        client = boto3.client(
            "bedrock-runtime",
            region_name=self.region,
            aws_access_key_id=creds["access_key"],
            aws_secret_access_key=creds["secret_key"],
            aws_session_token=creds.get("session_token") or None,
        )

        response = client.converse(
            modelId=self.model,
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            system=[{"text": system_prompt}],
            inferenceConfig={"maxTokens": 1024},
        )

        return response["output"]["message"]["content"][0]["text"]


class ClaudeCLIProvider(LLMProvider):
    """Uses the `claude` CLI in print mode (-p) for one-shot completions.

    This is the most reliable option on Amazon-managed machines where the
    claude CLI is already authenticated (via Midway/internal gateway) — it
    avoids direct Bedrock InvokeModel permissions entirely.

    NOTE FOR PRODUCTIZATION:
    - This is an Amazon-internal convenience (uses the work claude CLI).
    - For the public product, use ClaudeProvider (Anthropic API key) instead.
    - Not portable to personal Macs unless they have the same CLI installed.
    """
    def __init__(self, cli_path="/Users/rickvan/.toolbox/bin/claude"):
        self.cli_path = cli_path

    def chat(self, system_prompt: str, user_message: str) -> str:
        import subprocess
        combined = f"{system_prompt}\n\n{user_message}"
        result = subprocess.run(
            [self.cli_path, "-p", combined],
            capture_output=True, text=True, timeout=90
        )
        if result.returncode != 0:
            raise RuntimeError(f"claude CLI failed: {result.stderr[:200]}")
        # Filter out any 'claude: info:' lines that leak to stdout
        lines = [l for l in result.stdout.strip().split("\n") if not l.startswith("claude: info")]
        return "\n".join(lines).strip()


def create_provider(config: dict) -> LLMProvider:
    """Create LLM provider from config."""
    llm_config = config.get("llm", {})
    provider_name = llm_config.get("provider", "claude")
    model = llm_config.get("model")

    if provider_name == "claude":
        api_key = llm_config.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("Error: Set ANTHROPIC_API_KEY or api_key in config", file=sys.stderr)
            sys.exit(1)
        return ClaudeProvider(api_key, model or "claude-sonnet-4-20250514")
    elif provider_name == "openai":
        api_key = llm_config.get("api_key") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("Error: Set OPENAI_API_KEY or api_key in config", file=sys.stderr)
            sys.exit(1)
        return OpenAIProvider(api_key, model or "gpt-4o")
    elif provider_name == "ollama":
        return OllamaProvider(model or "llama3.1:8b")
    elif provider_name == "managed":
        api_url = llm_config.get("managed_api_url", "")
        api_key = llm_config.get("managed_api_key") or os.environ.get("MEETINGSTREAM_API_KEY")
        if not api_url or not api_key:
            print("Error: Set managed_api_url and managed_api_key for managed provider", file=sys.stderr)
            sys.exit(1)
        return ManagedProvider(api_url, api_key)
    elif provider_name == "bedrock":
        # Direct Bedrock InvokeModel — requires bedrock:InvokeModel IAM permission.
        # NOTE: Amazon-internal claude CLI credentials do NOT have this permission
        #       (they route through an internal gateway). Use "claude-cli" instead
        #       on work machines. This is for customers with their own AWS + Bedrock.
        region = llm_config.get("region", "us-west-2")
        credential_cmd = llm_config.get("credential_cmd")
        bedrock_model = model or "anthropic.claude-sonnet-4-20250514-v1:0"
        return BedrockProvider(bedrock_model, region, credential_cmd)
    elif provider_name == "claude-cli":
        # Amazon-internal: shells out to the authenticated claude CLI.
        # Free, no API key, works on work machines. NOT for public product.
        cli_path = llm_config.get("cli_path", "/Users/rickvan/.toolbox/bin/claude")
        return ClaudeCLIProvider(cli_path)
    else:
        print(f"Error: Unknown provider '{provider_name}'", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Learned Rules (persistent memory for routing decisions)
# ---------------------------------------------------------------------------

LEARNED_RULES_FILE = Path.home() / ".config" / "meetingstream" / "learned-routes.json"


def load_learned_rules() -> list[dict]:
    """Load previously learned routing rules."""
    if LEARNED_RULES_FILE.exists():
        return json.loads(LEARNED_RULES_FILE.read_text())
    return []


def save_learned_rule(title_pattern: str, destination: str, tag: str):
    """Save a new learned routing rule."""
    LEARNED_RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    rules = load_learned_rules()
    rules.append({
        "pattern": title_pattern.lower(),
        "destination": destination,
        "tag": tag,
        "learned_at": datetime.now().isoformat(),
        "confirmations": 1,
    })
    LEARNED_RULES_FILE.write_text(json.dumps(rules, indent=2))


def check_learned_rules(title: str):
    """Check if a learned rule matches this title.
    
    Validates that the destination path still exists on disk.
    If the folder has been renamed, moved, or deleted, the rule is
    invalidated (removed) and we fall through to later classification steps.
    
    TODO (UI build): In the Settings UI, show all learned rules with:
      - Edit/delete buttons per rule
      - Visual indicator for rules pointing to missing/deleted folders
      - Visual indicator for new folders that don't have any rules yet
      - "Re-evaluate" button that re-scans and flags discrepancies
      See: CONTEXT-UI-BUILD.md
    """
    rules = load_learned_rules()
    title_lower = title.lower()
    modified = False

    for rule in rules:
        if rule["pattern"] in title_lower:
            # Validate destination still exists
            if Path(rule["destination"]).exists():
                return rule
            else:
                # Path is stale — invalidate this rule
                print(f"[route] Learned rule invalidated: '{rule['pattern']}' → folder no longer exists: {rule['destination']}")
                rules.remove(rule)
                modified = True
                break  # Fall through to next classification step

    if modified:
        LEARNED_RULES_FILE.write_text(json.dumps(rules, indent=2))

    return None


# ---------------------------------------------------------------------------
# Folder Scanner
# ---------------------------------------------------------------------------

def scan_folder_structure(base_path: str, max_depth: int = 3) -> list[str]:
    """Scan user's folder structure to discover available destinations."""
    folders = []
    base = Path(base_path)
    if not base.exists():
        return folders

    for root, dirs, _files in os.walk(str(base)):
        depth = len(Path(root).relative_to(base).parts)
        if depth >= max_depth:
            dirs.clear()
            continue
        # Skip hidden folders and system folders
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
        rel_path = str(Path(root).relative_to(base))
        if rel_path != '.':
            folders.append(rel_path)

    return sorted(folders)


# ---------------------------------------------------------------------------
# Agentic Router
# ---------------------------------------------------------------------------

ROUTER_SYSTEM_PROMPT = """You are a meeting notes filing assistant. Your job is to classify a meeting and determine which project folder it belongs in.

You will receive:
1. The meeting title (from the user's calendar)
2. A summary of the meeting notes content
3. A list of available folders in the user's workspace

Respond with ONLY a JSON object (no markdown, no explanation):
{
    "folder": "exact/relative/path/from/the/folder/list",
    "tag": "ShortProjectName",
    "confidence": 0.0-1.0,
    "reasoning": "one sentence why"
}

Rules:
- Pick the MOST SPECIFIC folder that matches (prefer "Projects/Exit-OFA/Meeting Notes" over just "Projects")
- The "tag" should be a short project name for the filename (e.g., "Exit-OFA", "REMA", "1:1")
- If no folder is a good match, use "Unclassified" as the folder and tag
- Confidence: 0.9+ = certain, 0.7-0.9 = likely, below 0.7 = uncertain (will ask user)
"""


def agentic_classify(provider: LLMProvider, title: str, notes_content: str, folders: list[str]) -> dict:
    """Use LLM to classify the meeting and pick a destination folder."""
    # Truncate notes to first 2000 chars (enough for classification, saves tokens)
    notes_summary = notes_content[:2000] if len(notes_content) > 2000 else notes_content

    user_message = f"""Meeting title: {title}

Meeting notes summary:
{notes_summary}

Available folders:
{chr(10).join(f'- {f}' for f in folders[:50])}

Classify this meeting and pick the best folder."""

    response = provider.chat(ROUTER_SYSTEM_PROMPT, user_message)

    # Parse JSON response
    try:
        # Strip markdown code fences if present
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = "\n".join(cleaned.split("\n")[1:-1])
        result = json.loads(cleaned)
        return result
    except json.JSONDecodeError:
        return {"folder": "Unclassified", "tag": "Unclassified", "confidence": 0.0, "reasoning": "Failed to parse LLM response"}


# ---------------------------------------------------------------------------
# Deterministic Router (keyword-based, from routing.toml)
# ---------------------------------------------------------------------------

def deterministic_classify(title: str, config: dict):
    """Keyword-based classification from routing.toml. Returns None if no match."""
    base_path = config["routing"]["base_path"]

    # Check known series
    for series in config.get("known_series", []):
        if series["pattern"].lower() in title.lower():
            project_name = series["project"]
            for p in config.get("project", []):
                if p["name"] == project_name:
                    if p.get("folder") == "_ABSOLUTE_":
                        folder = p.get("absolute_path", "")
                    else:
                        folder = f"{base_path}/{p['folder']}"
                    sub = series.get("sub_route")
                    if sub:
                        for sr in p.get("sub_route", []):
                            if sr["name"] == sub:
                                return {"folder": f"{folder}/{sr['subfolder']}", "tag": project_name, "confidence": 1.0}
                    notes_sub = p.get("notes_subfolder", "")
                    dest = f"{folder}/{notes_sub}" if notes_sub else folder
                    return {"folder": dest, "tag": project_name, "confidence": 1.0}

    # Keyword matching
    for project in config.get("project", []):
        for keyword in project.get("keywords", []):
            if keyword.lower() in title.lower():
                if project.get("folder") == "_ABSOLUTE_":
                    folder = project.get("absolute_path", "")
                else:
                    folder = f"{base_path}/{project['folder']}"
                # Sub-route check
                for sr in project.get("sub_route", []):
                    for sr_kw in sr.get("keywords", []):
                        if sr_kw.lower() in title.lower():
                            return {"folder": f"{folder}/{sr['subfolder']}", "tag": project["name"], "confidence": 0.95}
                notes_sub = project.get("notes_subfolder", "")
                dest = f"{folder}/{notes_sub}" if notes_sub else folder
                return {"folder": dest, "tag": project["name"], "confidence": 0.95}

    return None  # No match


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_filename(meeting_date: str, tag: str, title: str, dest_folder: str) -> str:
    """Generate filename per convention: YYYY-MM-DD - [Tag] Title.md"""
    # Clean title for filename
    clean_title = title
    # Strip common prefixes that are now in the tag
    for prefix in ["OFA Exit", "OFA-AP exit", "Exit-OFA", "please prioritize"]:
        if clean_title.lower().startswith(prefix.lower()):
            clean_title = clean_title[len(prefix):].lstrip(" -[]")
    # Remove bracket content that matches tag
    import re
    clean_title = re.sub(r'\[(?:OFA[^]]*|Exit[^]]*)\]\s*', '', clean_title)
    # Sanitize
    clean_title = re.sub(r'[:\?\*"<>|]', '', clean_title).strip()
    clean_title = clean_title[:80]

    filename = f"{meeting_date} - [{tag}] {clean_title}.md"

    # Only add time if duplicate exists
    if Path(dest_folder).joinpath(filename).exists():
        time_str = datetime.now().strftime("%H-%M")
        filename = f"{meeting_date} {time_str} - [{tag}] {clean_title}.md"

    return filename


def main():
    parser = argparse.ArgumentParser(description="Route meeting notes to project folder")
    parser.add_argument("session_dir", help="Path to session output directory")
    parser.add_argument("--mode", choices=["agentic", "deterministic", "hybrid"], default="hybrid")
    parser.add_argument("--title", default=None, help="Meeting title (skips calendar lookup)")
    parser.add_argument("--config", default=None, help="Path to session.toml")
    parser.add_argument("--yes", action="store_true", help="Auto-confirm (don't ask)")
    args = parser.parse_args()

    session_dir = Path(args.session_dir).expanduser()
    notes_file = session_dir / "meeting-notes.md"

    if not notes_file.exists():
        print("[route] No meeting-notes.md found — skipping.")
        return

    # Load config
    script_dir = Path(__file__).parent.parent
    config_path = Path(args.config) if args.config else script_dir / "config" / "session.toml"
    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    # Get meeting title
    title = args.title or ""
    if not title:
        # Try to extract from notes header
        content = notes_file.read_text()
        for line in content.split("\n")[:5]:
            if line.startswith("# ") and "Meeting Notes" not in line:
                title = line.lstrip("# ").split(" — ")[0]
                break
        if not title:
            title = input("[route] Enter meeting title: ").strip()
            if not title:
                print("[route] No title — skipping.")
                return

    print(f"[route] Title: {title}")

    # Read notes content
    notes_content = notes_file.read_text()

    # Determine meeting date from folder name
    # Expected: .../YYYY-MM-DD/HH-MM/
    try:
        meeting_date = session_dir.parent.name  # YYYY-MM-DD
        datetime.strptime(meeting_date, "%Y-%m-%d")  # Validate
    except:
        meeting_date = datetime.now().strftime("%Y-%m-%d")

    # --- Classification ---
    result = None

    # Step 1: Check learned rules
    learned = check_learned_rules(title)
    if learned:
        result = {"folder": learned["destination"], "tag": learned["tag"], "confidence": 0.95,
                  "reasoning": "Matched learned rule"}
        print(f"[route] Learned rule match: [{result['tag']}]")

    # Step 2: Deterministic (if mode allows)
    if not result and args.mode in ("deterministic", "hybrid"):
        result = deterministic_classify(title, config)
        if result:
            print(f"[route] Deterministic match: [{result['tag']}] (confidence: {result['confidence']})")

    # Step 3: Agentic (if mode allows and no deterministic match)
    if not result and args.mode in ("agentic", "hybrid"):
        print("[route] No keyword match — asking LLM...")
        provider = create_provider(config)

        # Scan available folders
        base_path = config.get("routing", {}).get("base_path", str(Path.home() / "Documents"))
        folders = scan_folder_structure(base_path)

        result = agentic_classify(provider, title, notes_content, folders)
        print(f"[route] LLM classification: [{result['tag']}] (confidence: {result['confidence']:.2f})")
        print(f"[route] Reasoning: {result.get('reasoning', 'none')}")

        # Resolve relative folder path to absolute
        if not result["folder"].startswith("/"):
            result["folder"] = f"{base_path}/{result['folder']}"

    # Step 4: Fallback
    if not result:
        unclassified = config.get("routing", {}).get("unclassified_path",
                                                      str(Path.home() / "Documents" / "MeetingStream" / "Unclassified"))
        result = {"folder": unclassified, "tag": "Unclassified", "confidence": 0.0}
        print("[route] No classification — filing to Unclassified")

    # --- Confirmation (if low confidence and not --yes) ---
    if result["confidence"] < 0.7 and not args.yes:
        print(f"\n[route] Low confidence ({result['confidence']:.2f}). Proposed:")
        print(f"        Tag: [{result['tag']}]")
        print(f"        Folder: {result['folder']}")
        confirm = input("        Accept? [Y/n/custom folder path]: ").strip()
        if confirm.lower() == 'n':
            print("[route] Skipped.")
            return
        elif confirm and confirm.lower() != 'y':
            result["folder"] = confirm
            result["tag"] = input("        Tag for filename: ").strip() or "Custom"

    # --- File it ---
    dest_folder = result["folder"]
    tag = result["tag"]
    filename = generate_filename(meeting_date, tag, title, dest_folder)

    Path(dest_folder).mkdir(parents=True, exist_ok=True)
    dest_path = Path(dest_folder) / filename
    shutil.copy2(str(notes_file), str(dest_path))

    # Add session reference to filed copy
    with open(dest_path, "a") as f:
        f.write(f"\n\n---\n> Full session: `{session_dir}/`\n")

    print(f"[route] Filed: {filename}")
    print(f"[route] → {dest_folder}/")

    # --- Learn from this filing (for agentic mode) ---
    if args.mode in ("agentic", "hybrid") and result["confidence"] < 0.95:
        # Save as a learned rule for next time
        # Use a simplified title pattern (first meaningful words)
        words = [w for w in title.lower().split() if len(w) > 3][:3]
        pattern = " ".join(words) if words else title.lower()[:30]
        save_learned_rule(pattern, dest_folder, tag)
        print(f"[route] Learned: '{pattern}' → [{tag}]")


if __name__ == "__main__":
    main()
