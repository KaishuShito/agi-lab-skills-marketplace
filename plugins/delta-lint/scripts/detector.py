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


LANG_INSTRUCTIONS = {
    "en": "",  # default: no extra instruction, LLM writes English naturally
    "ja": (
        "## Language\n\n"
        "Write the `contradiction`, `impact`, and `internal_evidence` fields in **Japanese**. "
        "Keep `pattern`, `severity`, and `location` fields in English. "
        "Example: `\"impact\": \"デフォルト設定でLoRAファインチューニングを実行するとAttributeErrorでクラッシュする\"`"
    ),
}


def load_system_prompt(lang: str = "en", repo_path: str = "",
                       prompt_append: str = "",
                       lens: str = "default") -> str:
    """Load the detection system prompt.

    Resolution order:
    1. .delta-lint/detect.md (team override — full control, replaces core)
    2. Built-in prompts/detect.md (default)

    If lens="security", appends security-focused instructions.
    If prompt_append is provided (from policy.prompt_append), it is appended
    to whichever prompt is loaded.
    """
    # Check for team override
    if repo_path:
        override_path = Path(repo_path) / ".delta-lint" / "detect.md"
        if override_path.exists():
            prompt = override_path.read_text(encoding="utf-8")
            lang_instruction = LANG_INSTRUCTIONS.get(lang, "")
            prompt = prompt.replace("{lang_instruction}", lang_instruction)
            if lens == "security":
                prompt += "\n\n" + _SECURITY_LENS_APPEND
            if prompt_append:
                prompt += f"\n\n## Team-Specific Instructions\n\n{prompt_append}"
            return prompt

    # Default: built-in prompt
    prompt_path = PROMPT_DIR / "detect.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    lang_instruction = LANG_INSTRUCTIONS.get(lang, "")
    prompt = prompt.replace("{lang_instruction}", lang_instruction)
    if lens == "security":
        prompt += "\n\n" + _SECURITY_LENS_APPEND
    if prompt_append:
        prompt += f"\n\n## Team-Specific Instructions\n\n{prompt_append}"
    return prompt


_SECURITY_LENS_APPEND = """\
## Security Lens (Active)

You are now operating in **security-focused mode**. In addition to the standard
contradiction and debt patterns, prioritize detection of security-relevant issues:

### Security Patterns to Emphasize

1. **Authentication/Authorization Asymmetry**: One endpoint checks permissions,
   a sibling endpoint does not. Login path validates tokens differently than API path.
2. **Input Validation Gap**: One code path sanitizes input, another path reaching
   the same sink does not. SQL/command injection via unvalidated alternative route.
3. **Secret/Credential Leakage**: Config paths, API keys, tokens exposed in error
   messages, logs, or client-facing responses. Debug endpoints left enabled.
4. **Cryptographic Inconsistency**: One module uses constant-time comparison,
   another uses `==`. Hash algorithm mismatch between signing and verification.
5. **Race Condition in Security Check**: TOCTOU between permission check and
   privileged operation. Share counters without atomicity.

### Severity Mapping for Security Findings

- **high**: Exploitable without authentication, data leakage, privilege escalation
- **medium**: Requires authenticated access, defense-in-depth gaps
- **low**: Informational, hardening opportunities

Report security findings with the same JSON format. Use the most relevant
contradiction pattern (①-⑩) that matches, or describe the security-specific
mechanism in the `contradiction` field.
"""


def build_user_prompt(context: ModuleContext, repo_name: str = "",
                      constraints: list[dict] | None = None,
                      architecture: list[str] | None = None,
                      diff_text: str = "",
                      project_rules: list[str] | None = None) -> str:
    """Build the user prompt with code context.

    Args:
        context: ModuleContext from retrieval layer
        repo_name: Optional repository name
        constraints: Optional list of {path, implicit_constraints} dicts
            from structure.json. Only constraints relevant to target files
            are included — these represent known invariants that may not
            be obvious from the code alone (e.g. veteran knowledge).
        architecture: Optional list of architectural context strings from
            team policy. These describe intentional design decisions that
            should NOT be flagged as contradictions.
        diff_text: Optional git diff output showing exactly which lines changed.
        project_rules: Optional list of project-specific rules that describe
            domain knowledge, naming conventions, or design patterns that
            should NOT be flagged as contradictions.
    """
    header = "Analyze the following source code files for structural contradictions.\n"
    if repo_name:
        header += f"Repository: {repo_name}\n"
    header += (
        "These files are from related modules in the codebase. "
        "Look for contradictions BETWEEN different files/functions — "
        "places where one module's assumptions contradict another module's behavior.\n\n"
    )
    if context.doc_files:
        header += (
            "**Document contract surfaces are included below** (marked as DOCUMENT). "
            "These represent specifications, architecture decisions, or README claims. "
            "Also check whether the source code contradicts what the documents promise.\n\n"
        )

    prompt = header + context.to_prompt_string()

    # Inject diff context — shows LLM exactly which lines were changed
    if diff_text:
        prompt += "\n\n## Recent Changes (git diff)\n\n"
        prompt += (
            "The following diff shows the exact lines that were recently modified. "
            "Focus your analysis on these changes and their siblings: "
            "if a function was updated here, check whether its counterparts "
            "(parallel handlers, paired serializers, shared-contract functions) "
            "are still consistent.\n\n"
        )
        prompt += f"```diff\n{diff_text}\n```"

    # Inject architectural context (team policy — reduces false positives)
    if architecture:
        prompt += "\n\n## Architectural Context (team policy)\n\n"
        prompt += (
            "The following are **intentional design decisions** by this team. "
            "Do NOT flag these as contradictions — they are accepted trade-offs.\n\n"
        )
        for item in architecture:
            prompt += f"- {item}\n"

    # Inject known constraints if available
    if constraints:
        prompt += "\n\n## Known Constraints (from project knowledge base)\n\n"
        prompt += (
            "The following implicit constraints have been identified for these modules. "
            "Use them to detect contradictions more accurately — a code change that "
            "violates these constraints is a strong signal of a structural contradiction.\n\n"
        )
        for c in constraints:
            path = c.get("path", "")
            items = c.get("implicit_constraints", [])
            if items:
                prompt += f"**{path}**:\n"
                for item in items:
                    prompt += f"- {item}\n"
                prompt += "\n"

    # Inject project-specific rules (domain knowledge — reduces false positives)
    if project_rules:
        prompt += "\n\n## Project Rules (domain knowledge)\n\n"
        prompt += (
            "The following are project-specific facts about this codebase. "
            "Do NOT flag these as contradictions — they reflect domain knowledge "
            "that is not obvious from the code alone.\n\n"
        )
        for rule in project_rules:
            prompt += f"- {rule}\n"

    return prompt


def load_policy(repo_path: str) -> dict:
    """Load team policy from constraints.yml.

    Returns dict with optional keys:
      architecture: list[str]  — context for LLM (injected into prompt)
      accepted: list[dict]     — findings to suppress (id or pattern+file)
      severity_overrides: list[dict] — severity adjustments per pattern/file
      debt_budget: float|None  — max allowed active debt score (CI gate)
    """
    constraints_path = Path(repo_path) / ".delta-lint" / "constraints.yml"
    if not constraints_path.exists():
        return {}

    try:
        import yaml
    except ImportError:
        return {}

    try:
        data = yaml.safe_load(constraints_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    return (data or {}).get("policy", {})


def load_constraints(repo_path: str, target_files: list[str]) -> list[dict]:
    """Load implicit constraints for target files.

    Merges two sources:
    1. .delta-lint/constraints.yml — manual/Claude Code entries (takes priority)
    2. .delta-lint/stress-test/structure.json — LLM auto-extracted

    constraints.yml is never overwritten by delta init.
    structure.json is regenerated on every delta init.

    Returns list of {path, implicit_constraints, source} dicts.
    """
    all_constraints: dict[str, dict] = {}  # path -> {constraints: [], source: str}

    # 1. Load structure.json (auto-extracted, lower priority)
    structure_path = Path(repo_path) / ".delta-lint" / "stress-test" / "structure.json"
    if structure_path.exists():
        try:
            data = json.loads(structure_path.read_text(encoding="utf-8"))
            for mod in data.get("modules", []):
                mod_path = mod.get("path", "")
                items = mod.get("implicit_constraints", [])
                if mod_path and items:
                    all_constraints[mod_path] = {
                        "implicit_constraints": items,
                        "source": "auto",
                    }
        except (json.JSONDecodeError, OSError):
            pass

    # 2. Load constraints.yml (manual entries, higher priority — overwrites auto)
    constraints_path = Path(repo_path) / ".delta-lint" / "constraints.yml"
    if constraints_path.exists():
        try:
            import yaml
        except ImportError:
            yaml = None
        if yaml:
            try:
                data = yaml.safe_load(constraints_path.read_text(encoding="utf-8"))
                for entry in (data or {}).get("constraints", []):
                    path = entry.get("file", "")
                    items = entry.get("rules", [])
                    if path and items:
                        if path in all_constraints:
                            # Merge: manual rules prepended, auto rules appended
                            existing = all_constraints[path]["implicit_constraints"]
                            merged = items + [r for r in existing if r not in items]
                            all_constraints[path] = {
                                "implicit_constraints": merged,
                                "source": "manual+auto",
                            }
                        else:
                            all_constraints[path] = {
                                "implicit_constraints": items,
                                "source": "manual",
                            }
            except Exception:
                pass

    if not all_constraints:
        return []

    # Filter to constraints relevant to target files
    target_set = set()
    for f in target_files:
        target_set.add(f)
        if f.startswith("./"):
            target_set.add(f[2:])

    relevant = []
    for mod_path, info in all_constraints.items():
        is_match = mod_path in target_set or any(
            mod_path.endswith(t) or t.endswith(mod_path) for t in target_set
        )
        if is_match:
            relevant.append({
                "path": mod_path,
                "implicit_constraints": info["implicit_constraints"],
                "source": info["source"],
            })

    return relevant


# ---------------------------------------------------------------------------
# LLM backends
# ---------------------------------------------------------------------------

def detect(context: ModuleContext, repo_name: str = "",
           model: str = "claude-sonnet-4-20250514",
           backend: str = "cli",
           lang: str = "en",
           constraints: list[dict] | None = None,
           architecture: list[str] | None = None,
           diff_text: str = "",
           project_rules: list[str] | None = None,
           repo_path: str = "",
           prompt_append: str = "",
           disabled_patterns: list[str] | None = None,
           detect_prompt: str = "",
           lens: str = "default") -> list[dict]:
    """Run contradiction detection on a module context.

    Args:
        context: ModuleContext from retrieval layer
        repo_name: Optional repository name for context
        model: Model identifier
        backend: "cli" (claude -p, $0, default), "api" (SDK/HTTP, pay-per-use)
        lang: Output language for descriptive fields ("en" or "ja")
        constraints: Optional implicit constraints from structure.json
        architecture: Optional architectural context from team policy
        diff_text: Optional git diff output for change-aware detection
        project_rules: Optional project-specific domain rules
        repo_path: Optional repo path for loading team prompt override
        prompt_append: Optional text appended to system prompt (from policy)
        disabled_patterns: Optional list of pattern IDs to skip (e.g. ["⑦", "⑩"])
        detect_prompt: Optional custom system prompt (overrides detect.md entirely)
        lens: Detection lens — "default", "security", or "stress"

    Returns:
        List of contradiction dicts (raw from LLM, unfiltered)
    """
    if detect_prompt:
        # Profile-provided custom prompt — bypass detect.md loading
        system_prompt = detect_prompt
        if prompt_append:
            system_prompt += f"\n\n## Team-Specific Instructions\n\n{prompt_append}"
    else:
        system_prompt = load_system_prompt(lang=lang, repo_path=repo_path,
                                           prompt_append=prompt_append,
                                           lens=lens)
    user_prompt = build_user_prompt(context, repo_name, constraints=constraints,
                                    architecture=architecture,
                                    diff_text=diff_text,
                                    project_rules=project_rules)

    if backend == "cli" and not _cli_available():
        backend = "api"

    if backend == "cli":
        raw = _detect_cli(system_prompt, user_prompt)
    elif anthropic is not None:
        raw = _detect_anthropic_sdk(system_prompt, user_prompt, model)
    elif req_lib is not None:
        raw = _detect_requests(system_prompt, user_prompt, model)
    else:
        raise RuntimeError(
            "No backend available. Install 'anthropic' package or ensure "
            "'claude' CLI is on PATH."
        )

    findings = _parse_response(raw)

    # Filter out disabled patterns
    if disabled_patterns:
        disabled = set(disabled_patterns)
        findings = [f for f in findings if f.get("pattern", "") not in disabled]

    return findings


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


def _detect_cli(system_prompt: str, user_prompt: str) -> str:
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


def _detect_anthropic_sdk(system_prompt: str, user_prompt: str, model: str) -> str:
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
