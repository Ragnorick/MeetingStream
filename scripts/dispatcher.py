#!/usr/bin/env python3
"""
MeetingStream Dispatcher
Replaces TalkStream's agent dispatch layer using Claude/OpenAI APIs.

Reads real-time transcription chunks from stdin (piped from whisper-stream.sh)
and dispatches to configured agents (note-taker, transcriber) in parallel.

Usage:
    ./whisper-stream.sh | python3 dispatcher.py --config ../config/session.toml
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # Python < 3.11


# --- LLM Provider Interface ---

class LLMProvider:
    """Base class for LLM providers."""
    def chat(self, system_prompt: str, messages: list[dict]) -> str:
        raise NotImplementedError


class ClaudeProvider(LLMProvider):
    """Anthropic Claude API provider."""
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        try:
            import anthropic
        except ImportError:
            print("Error: pip install anthropic")
            sys.exit(1)
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def chat(self, system_prompt: str, messages: list[dict]) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system_prompt,
            messages=messages,
        )
        return response.content[0].text


class OpenAIProvider(LLMProvider):
    """OpenAI API provider."""
    def __init__(self, api_key: str, model: str = "gpt-4o"):
        try:
            import openai
        except ImportError:
            print("Error: pip install openai")
            sys.exit(1)
        self.client = openai.OpenAI(api_key=api_key)
        self.model = model

    def chat(self, system_prompt: str, messages: list[dict]) -> str:
        msgs = [{"role": "system", "content": system_prompt}] + messages
        response = self.client.chat.completions.create(
            model=self.model,
            messages=msgs,
            max_tokens=4096,
        )
        return response.choices[0].message.content


class OllamaProvider(LLMProvider):
    """Local Ollama provider (free, no API key)."""
    def __init__(self, model: str = "llama3.1:8b", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url

    def chat(self, system_prompt: str, messages: list[dict]) -> str:
        import urllib.request
        import json as _json

        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}] + messages,
            "stream": False,
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=_json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = _json.loads(resp.read())
        return result["message"]["content"]


# --- Agent ---

class Agent:
    """An agent that maintains conversation history and writes to files."""

    def __init__(self, name: str, system_prompt: str, output_dir: Path, provider: LLMProvider):
        self.name = name
        self.system_prompt = system_prompt
        self.output_dir = output_dir
        self.provider = provider
        self.messages: list[dict] = []
        self.lock = threading.Lock()

    def dispatch(self, text: str):
        """Send a chunk of text to the agent and let it update its output file."""
        with self.lock:
            self.messages.append({"role": "user", "content": text})
            try:
                response = self.provider.chat(self.system_prompt, self.messages)
                self.messages.append({"role": "assistant", "content": response})
                # Agent writes files via instructions in its system prompt
                # We also handle file writes from the response if structured
                self._handle_response(response)
            except Exception as e:
                print(f"  [{self.name}] Error: {e}", file=sys.stderr)

    def _handle_response(self, response: str):
        """Write agent output to its designated file."""
        output_file = self.output_dir / self._get_filename()
        if self.name == "transcriber":
            # Append mode for transcriber
            with open(output_file, "a") as f:
                # Extract just the content lines from the response
                for line in response.strip().split("\n"):
                    if line.strip() and line.strip().lower() != "done":
                        f.write(line + "\n")
        else:
            # Full rewrite for note-taker and sketch-artist
            content = response.strip()
            if content.lower() != "done" and len(content) > 20:
                with open(output_file, "w") as f:
                    f.write(content + "\n")

    def _get_filename(self) -> str:
        if self.name == "transcriber":
            return "transcript.md"
        elif self.name == "note-taker":
            return "meeting-notes.md"
        elif self.name == "sketch-artist":
            return "sketch.md"
        return f"{self.name}.md"


# --- Router ---

class Router:
    """Simple keyword-based router that decides which agents get each chunk."""

    def __init__(self, agents: list[Agent]):
        self.agents = {a.name: a for a in agents}

    def route(self, text: str) -> list[Agent]:
        """Route text to appropriate agents."""
        targets = []
        # Transcriber always gets everything
        if "transcriber" in self.agents:
            targets.append(self.agents["transcriber"])
        # Note-taker gets substantive content (skip very short fragments)
        if "note-taker" in self.agents and len(text.split()) > 5:
            targets.append(self.agents["note-taker"])
        # Sketch-artist only on architecture keywords
        if "sketch-artist" in self.agents:
            arch_keywords = ["architecture", "diagram", "system design", "flow",
                           "service", "database", "api", "endpoint", "microservice",
                           "component", "layer", "pipeline"]
            if any(kw in text.lower() for kw in arch_keywords):
                targets.append(self.agents["sketch-artist"])
        return targets


# --- Main ---

def load_config(config_path: str) -> dict:
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def create_provider(config: dict) -> LLMProvider:
    """Create LLM provider from config."""
    provider_name = config.get("provider", "claude")
    model = config.get("model", None)

    if provider_name == "claude":
        api_key = config.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("Error: Set ANTHROPIC_API_KEY env var or api_key in config")
            sys.exit(1)
        return ClaudeProvider(api_key, model or "claude-sonnet-4-20250514")

    elif provider_name == "openai":
        api_key = config.get("api_key") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("Error: Set OPENAI_API_KEY env var or api_key in config")
            sys.exit(1)
        return OpenAIProvider(api_key, model or "gpt-4o")

    elif provider_name == "ollama":
        return OllamaProvider(model or "llama3.1:8b")

    else:
        print(f"Error: Unknown provider '{provider_name}'")
        sys.exit(1)


def load_agent_prompt(agents_dir: Path, agent_name: str) -> str:
    """Load agent system prompt from file."""
    prompt_file = agents_dir / f"{agent_name}.md"
    if prompt_file.exists():
        return prompt_file.read_text()
    # Fallback defaults
    defaults = {
        "transcriber": "You are a real-time verbatim transcriber. Append each utterance you receive to your output. Format: [HH:MM:SS] **Speaker:** text. Never ask questions. Only output transcript lines.",
        "note-taker": "You are a meeting note-taker. On each new input, rewrite the complete meeting notes incorporating all information received so far. Format: Summary, Key Decisions, Action Items, Discussion.",
        "sketch-artist": "You are a diagram generator. When you receive architecture discussion, output a Mermaid diagram. Only output the mermaid code block.",
    }
    return defaults.get(agent_name, f"You are a helpful assistant named {agent_name}.")


def main():
    parser = argparse.ArgumentParser(description="MeetingStream Dispatcher")
    parser.add_argument("--config", default="config/session.toml", help="Path to session config")
    parser.add_argument("--output", default=None, help="Output directory (default: ~/Documents/MeetingStream/YYYY-MM-DD/HH-MM/)")
    args = parser.parse_args()

    # Load config
    script_dir = Path(__file__).parent.parent
    config_path = Path(args.config) if Path(args.config).is_absolute() else script_dir / args.config
    config = load_config(str(config_path))

    # Set up output directory
    if args.output:
        output_dir = Path(args.output).expanduser()
    else:
        now = datetime.now()
        output_dir = Path.home() / "Documents" / "MeetingStream" / now.strftime("%Y-%m-%d") / now.strftime("%H-%M")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Create LLM provider
    provider = create_provider(config.get("llm", {}))

    # Create agents
    agents_dir = script_dir / "agents"
    agent_names = config.get("agents", ["transcriber", "note-taker"])
    agents = []
    for name in agent_names:
        prompt = load_agent_prompt(agents_dir, name)
        agents.append(Agent(name, prompt, output_dir, provider))

    # Create router
    router = Router(agents)

    # Print startup info
    print(f"MeetingStream ready")
    print(f"  Agents: {', '.join(agent_names)}")
    print(f"  Output: {output_dir}")
    print(f"  Provider: {config.get('llm', {}).get('provider', 'claude')}")
    print(f"  Waiting for transcription input on stdin...")
    print()

    # Read from stdin and dispatch
    buffer = []
    last_dispatch = time.time()

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            buffer.append(line)

            # Batch: dispatch every 3 seconds or every 3 lines
            if len(buffer) >= 3 or (time.time() - last_dispatch) > 3:
                text = "\n".join(buffer)
                buffer = []
                last_dispatch = time.time()

                # Route and dispatch in parallel
                targets = router.route(text)
                threads = []
                for agent in targets:
                    t = threading.Thread(target=agent.dispatch, args=(text,))
                    t.start()
                    threads.append(t)

                # Print dispatch info
                target_names = [a.name for a in targets]
                print(f"  [{datetime.now().strftime('%H:%M:%S')}] → {target_names}")

                # Don't wait for threads — let them run async
                # Agents will write to files when they get responses

    except KeyboardInterrupt:
        # Flush remaining buffer
        if buffer:
            text = "\n".join(buffer)
            targets = router.route(text)
            for agent in targets:
                agent.dispatch(text)

        print(f"\nSession ended. Files saved to: {output_dir}")


if __name__ == "__main__":
    main()
