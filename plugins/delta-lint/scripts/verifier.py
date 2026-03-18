"""
Verification layer for delta-lint (Phase 2 of two-stage pipeline).

Phase 1 (detector.py): Broad detection — prioritizes recall over precision.
Phase 2 (this file):   Strict verification — rejects false positives.

The verifier receives raw findings + source code context, then asks the LLM
to confirm or reject each finding with a reason and confidence score.

Design decisions:
- All findings are batched into ONE LLM call (not per-finding)
- Same backend fallback as detector.py: cli ($0) → api (pay-per-use)
- confidence < threshold → rejected (default threshold: 0.7)
- Rejected findings are returned separately for logging, not discarded
"""

import json
import os
import subprocess
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


def _load_verify_prompt() -> str:
    """Load the verification system prompt from prompts/verify.md."""
    return (PROMPT_DIR / "verify.md").read_text(encoding="utf-8")


def _build_verify_user_prompt(findings: list[dict], context: ModuleContext) -> str:
    """Build user prompt with findings to verify + source code context."""
    parts = [
        "## Findings to Verify\n\n",
        "```json\n",
        json.dumps(findings, indent=2, ensure_ascii=False),
        "\n```\n\n",
        "## Source Code\n\n",
        context.to_prompt_string(),
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# LLM backends (same pattern as detector.py)
# ---------------------------------------------------------------------------

def _cli_available() -> bool:
    """Check if claude CLI is available on PATH."""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _verify_cli(system_prompt: str, user_prompt: str) -> str:
    """Call Claude via claude -p (subscription CLI, $0 cost)."""
    prompt = system_prompt + "\n\n" + user_prompt
    result = subprocess.run(
        ["claude", "-p"],
        input=prompt,
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p failed: {result.stderr[:300]}")
    return result.stdout


def _verify_anthropic_sdk(system_prompt: str, user_prompt: str, model: str) -> str:
    """Call Claude via the official Anthropic SDK."""
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return message.content[0].text


def _verify_requests(system_prompt: str, user_prompt: str, model: str) -> str:
    """Call Claude via raw HTTP (fallback if SDK not installed)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY or CLAUDE_API_KEY not set")

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
            "temperature": 0,
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

def _parse_verdicts(raw: str) -> list[dict]:
    """Parse LLM response into list of verdict dicts."""
    import re

    text = raw.strip()

    # Extract JSON from markdown code block
    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    # Try to find JSON array in the text
    bracket_start = text.find("[")
    bracket_end = text.rfind("]")
    if bracket_start >= 0 and bracket_end > bracket_start:
        try:
            parsed = json.loads(text[bracket_start:bracket_end + 1])
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def verify_findings(
    findings: list[dict],
    context: ModuleContext,
    model: str = "claude-sonnet-4-20250514",
    backend: str = "cli",
    confidence_threshold: float = 0.7,
    verbose: bool = False,
) -> tuple[list[dict], list[dict], dict]:
    """Verify findings from Phase 1 detection.

    Args:
        findings: Raw findings from detect()
        context: ModuleContext with source code
        model: Model identifier
        backend: "cli" ($0) or "api" (pay-per-use)
        confidence_threshold: Min confidence to accept (default 0.7)
        verbose: Print progress to stderr

    Returns:
        (confirmed, rejected, meta)
        - confirmed: Findings that passed verification
        - rejected: Findings that failed verification (with _verify_reason)
        - meta: {"total", "confirmed", "rejected", "verdicts": [...]}
    """
    if not findings:
        return [], [], {"total": 0, "confirmed": 0, "rejected": 0, "verdicts": []}

    # Skip parse_error findings — they can't be verified
    verifiable = [f for f in findings if not f.get("parse_error")]
    passthrough = [f for f in findings if f.get("parse_error")]

    if not verifiable:
        return passthrough, [], {
            "total": len(findings), "confirmed": len(passthrough),
            "rejected": 0, "verdicts": [],
        }

    system_prompt = _load_verify_prompt()
    user_prompt = _build_verify_user_prompt(verifiable, context)

    if verbose:
        import sys
        print(f"  Verifying {len(verifiable)} finding(s)...", file=sys.stderr)

    # Call LLM
    if backend == "cli" and not _cli_available():
        backend = "api"

    if backend == "cli":
        raw = _verify_cli(system_prompt, user_prompt)
    elif anthropic is not None:
        raw = _verify_anthropic_sdk(system_prompt, user_prompt, model)
    elif req_lib is not None:
        raw = _verify_requests(system_prompt, user_prompt, model)
    else:
        # No backend available — pass all through (degrade gracefully)
        if verbose:
            import sys
            print("  No LLM backend for verification — skipping", file=sys.stderr)
        return findings, [], {
            "total": len(findings), "confirmed": len(findings),
            "rejected": 0, "verdicts": [], "skipped": True,
        }

    verdicts = _parse_verdicts(raw)

    # Build verdict lookup by index
    verdict_map: dict[int, dict] = {}
    for v in verdicts:
        idx = v.get("index")
        if idx is not None:
            verdict_map[idx] = v

    confirmed = list(passthrough)  # parse_error findings always pass through
    rejected = []

    for i, finding in enumerate(verifiable):
        v = verdict_map.get(i)
        if v is None:
            # LLM didn't return a verdict for this finding — keep it (conservative)
            confirmed.append(finding)
            continue

        verdict = v.get("verdict", "").lower()
        confidence = float(v.get("confidence", 0))

        if verdict == "confirmed" and confidence >= confidence_threshold:
            finding["_verify_confidence"] = confidence
            confirmed.append(finding)
        else:
            finding["_verify_verdict"] = verdict
            finding["_verify_confidence"] = confidence
            finding["_verify_reason"] = v.get("reason", "")
            rejected.append(finding)

    meta = {
        "total": len(findings),
        "confirmed": len(confirmed),
        "rejected": len(rejected),
        "verdicts": verdicts,
    }

    if verbose:
        import sys
        print(f"  Verified: {meta['confirmed']} confirmed, "
              f"{meta['rejected']} rejected", file=sys.stderr)

    return confirmed, rejected, meta
