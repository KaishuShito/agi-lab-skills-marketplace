"""
Detection layer for delta-lint MVP.

Calls LLM with the detection prompt and code context.
Returns raw JSON response from the LLM.

Design decisions:
- Claude Sonnet 4+ required (Experiment 1: qwen 25% vs Claude 42%)
- LLM outputs ALL findings with severity; filtering is done in output.py
- Structured JSON output for machine-parseable results
"""

import json
import os
from pathlib import Path

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    import requests as req_lib
except ImportError:
    req_lib = None

from retrieval import ModuleContext


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

PROMPT_DIR = Path(__file__).parent / "prompts"


def load_system_prompt() -> str:
    """Load the detection system prompt from prompts/detect.md."""
    prompt_path = PROMPT_DIR / "detect.md"
    return prompt_path.read_text(encoding="utf-8")


def build_user_prompt(context: ModuleContext, repo_name: str = "") -> str:
    """Build the user prompt with code context."""
    header = "Analyze the following source code files for structural contradictions.\n"
    if repo_name:
        header += f"Repository: {repo_name}\n"
    header += (
        "These files are from related modules in the codebase. "
        "Look for contradictions BETWEEN different files/functions — "
        "places where one module's assumptions contradict another module's behavior.\n\n"
    )
    return header + context.to_prompt_string()


# ---------------------------------------------------------------------------
# LLM backends
# ---------------------------------------------------------------------------

def detect(context: ModuleContext, repo_name: str = "",
           model: str = "claude-sonnet-4-20250514") -> list[dict]:
    """Run contradiction detection on a module context.

    Args:
        context: ModuleContext from retrieval layer
        repo_name: Optional repository name for context
        model: Model identifier

    Returns:
        List of contradiction dicts (raw from LLM, unfiltered)
    """
    system_prompt = load_system_prompt()
    user_prompt = build_user_prompt(context, repo_name)

    if anthropic is not None:
        raw = _detect_anthropic_sdk(system_prompt, user_prompt, model)
    elif req_lib is not None:
        raw = _detect_requests(system_prompt, user_prompt, model)
    else:
        raise RuntimeError(
            "Either 'anthropic' or 'requests' package is required. "
            "Install with: pip install anthropic"
        )

    return _parse_response(raw)


def _detect_anthropic_sdk(system_prompt: str, user_prompt: str, model: str) -> str:
    """Call Claude via the official Anthropic SDK."""
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return message.content[0].text


def _detect_requests(system_prompt: str, user_prompt: str, model: str) -> str:
    """Call Claude via raw HTTP (fallback if SDK not installed)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY or CLAUDE_API_KEY environment variable not set")

    resp = req_lib.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=180,
    )

    if resp.status_code != 200:
        raise RuntimeError(f"Claude API error {resp.status_code}: {resp.text[:300]}")

    return resp.json()["content"][0]["text"]


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_response(raw: str) -> list[dict]:
    """Parse LLM response into list of contradiction dicts.

    Handles both clean JSON and JSON embedded in markdown code blocks.
    """
    text = raw.strip()

    # Try to extract JSON from markdown code block
    if "```" in text:
        # Find the JSON block
        import re
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    # Handle empty result
    if text == "[]":
        return []

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass

    # If JSON parsing fails, try to find JSON array in the text
    bracket_start = text.find("[")
    bracket_end = text.rfind("]")
    if bracket_start >= 0 and bracket_end > bracket_start:
        try:
            parsed = json.loads(text[bracket_start:bracket_end + 1])
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    # Last resort: return raw text as a single unstructured finding
    return [{"raw_response": raw, "parse_error": True}]
