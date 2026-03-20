"""
Fix generation module for DeltaRegret.

Generates minimal code fixes for detected structural contradictions.
Shared between CLI (cli.py) and GitHub Action (action/entrypoint.py).

Supports two backends:
- cli: claude -p ($0, subscription CLI) — default
- api: Anthropic SDK (pay-per-use) — fallback
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import anthropic
except ImportError:
    anthropic = None


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

FIX_PROMPT = """\
You are a code fix generator. Given a structural contradiction between two files,
generate the minimal fix to resolve it.

## Rules
- Fix ONLY the contradiction described. Do not refactor or improve other code.
- Prefer fixing the file that deviates from the established pattern/config.
- Output valid JSON only.

## Contradiction
{contradiction_json}

## Source files
{source_code}

## Output Format
Return a JSON array of fixes. Each fix:
```json
[
  {{
    "file": "path/to/file.py",
    "line": 8,
    "old_code": "exact line(s) to replace",
    "new_code": "replacement line(s)",
    "explanation": "brief explanation of the fix"
  }}
]
```

If the fix requires changes in multiple places, include multiple entries.
If you cannot generate a safe fix, return `[]`.
"""


# ---------------------------------------------------------------------------
# Backend detection (reuse detector.py pattern)
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


# ---------------------------------------------------------------------------
# Fix generation
# ---------------------------------------------------------------------------

def generate_fixes(findings: list[dict], context, model: str,
                   backend: str = "cli", verbose: bool = False) -> list[dict]:
    """Generate minimal fixes for each finding.

    Args:
        findings: list of finding dicts from detector
        context: ModuleContext (has .to_prompt_string())
        model: model identifier
        backend: "cli" ($0) or "api" (pay-per-use)
        verbose: print progress to stderr

    Returns:
        list of fix dicts [{file, line, old_code, new_code, explanation, _finding}]
    """
    if backend == "cli" and not _cli_available():
        if verbose:
            print("  claude CLI not available, falling back to API", file=sys.stderr)
        backend = "api"

    source_code = context.to_prompt_string() if hasattr(context, 'to_prompt_string') else ""
    all_fixes = []

    for i, finding in enumerate(findings):
        if finding.get("parse_error"):
            continue

        if verbose:
            pattern = finding.get("pattern", "?")
            print(f"  Generating fix {i+1}/{len(findings)}: {pattern}...", file=sys.stderr)

        prompt = FIX_PROMPT.format(
            contradiction_json=json.dumps(finding, indent=2, ensure_ascii=False),
            source_code=source_code,
        )

        try:
            if backend == "cli":
                raw = _generate_fix_cli(prompt)
            else:
                raw = _generate_fix_api(prompt, model)

            fixes = _parse_fixes(raw)
            for fix in fixes:
                fix["_finding"] = finding
            all_fixes.extend(fixes)

            if verbose:
                print(f"    → {len(fixes)} fix(es) generated", file=sys.stderr)
        except Exception as e:
            print(f"  Fix generation failed: {e}", file=sys.stderr)

    return all_fixes


def _generate_fix_cli(prompt: str) -> str:
    """Call claude -p for fix generation ($0)."""
    result = subprocess.run(
        ["claude", "-p"],
        input=prompt,
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p failed: {result.stderr[:300]}")
    return result.stdout


def _generate_fix_api(prompt: str, model: str) -> str:
    """Call Anthropic API for fix generation."""
    if anthropic is None:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")

    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    client = anthropic.Anthropic(api_key=api_key, timeout=600.0) if api_key else anthropic.Anthropic(timeout=600.0)

    message = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_fixes(raw: str) -> list[dict]:
    """Parse fix JSON from LLM response."""
    text = raw.strip()

    # Extract from markdown code block
    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    # Try direct JSON parse
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass

    # Try bracket extraction
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
# Fix application (local only, no git commit/push)
# ---------------------------------------------------------------------------

def apply_fixes_locally(fixes: list[dict], repo_path: str,
                        verbose: bool = False) -> list[dict]:
    """Apply fixes to local files without git operations.

    Args:
        fixes: list of fix dicts from generate_fixes()
        repo_path: repository root path
        verbose: print progress to stderr

    Returns:
        list of successfully applied fixes
    """
    applied = []

    for fix in fixes:
        file_path = fix.get("file", "")
        old_code = fix.get("old_code", "")
        new_code = fix.get("new_code", "")

        if not file_path or not old_code:
            continue

        full_path = Path(repo_path) / file_path
        if not full_path.exists():
            if verbose:
                print(f"    Skip: {file_path} not found", file=sys.stderr)
            continue

        content = full_path.read_text(encoding="utf-8")

        # Try exact match
        if old_code in content:
            content = content.replace(old_code, new_code, 1)
            full_path.write_text(content, encoding="utf-8")
            applied.append(fix)
            if verbose:
                print(f"    Applied: {file_path}:{fix.get('line', '?')}", file=sys.stderr)
            continue

        # Retry with stripped trailing whitespace
        old_stripped = "\n".join(line.rstrip() for line in old_code.splitlines())
        content_stripped = "\n".join(line.rstrip() for line in content.splitlines())
        if old_stripped in content_stripped:
            content = content_stripped.replace(old_stripped, new_code, 1)
            full_path.write_text(content, encoding="utf-8")
            applied.append(fix)
            if verbose:
                print(f"    Applied (whitespace-normalized): {file_path}:{fix.get('line', '?')}",
                      file=sys.stderr)
            continue

        if verbose:
            print(f"    Skip: old_code not found in {file_path}", file=sys.stderr)

    return applied
