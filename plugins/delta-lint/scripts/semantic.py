"""
Semantic search layer for delta-lint.

Expands the scan context beyond import-based 1-hop dependencies
by asking an LLM to extract implicit assumptions from the diff,
then grepping the codebase for related files.

Design decisions:
- Uses `claude -p` (subscription CLI) for $0 cost
- Runs as a separate step after build_context (does not pollute retrieval layer)
- Merges results into existing ModuleContext
- Filters with source_exts + test exclusion (same as retrieval.py)
- Caps semantic deps to avoid context explosion
"""

import json
import re
import subprocess
from pathlib import Path

from retrieval import (
    ModuleContext,
    FileContext,
    filter_source_files,
    _read_file_safe,
    MAX_FILE_CHARS,
    MAX_CONTEXT_CHARS,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAX_SEMANTIC_DEPS = 8          # Max files to add from semantic search
ASSUMPTION_PROMPT = """\
You are analyzing a code diff to identify implicit assumptions and constraints.

Given the following diff, list the implicit assumptions this code change relies on.
Focus on cross-module dependencies — things that OTHER files in the codebase
might need to agree on for this change to be correct.

Examples of implicit assumptions:
- "field X is always present in the object returned by module Y"
- "function Z is never called with null"
- "config value W must match between service A and service B"
- "this enum value is handled in all switch statements"

Output a JSON array of objects, each with:
- "assumption": one-sentence description of the implicit assumption
- "search_patterns": array of 1-3 grep patterns (regex) to find related code.
  Use specific patterns like "fieldName.*=", not generic words.
  Combine terms to reduce noise (e.g., "mode.*webhook" not just "mode").

Output ONLY the JSON array, no other text.

Diff:
{diff}
"""


# ---------------------------------------------------------------------------
# Step 1: Get diff content
# ---------------------------------------------------------------------------

def get_diff_content(repo_path: str, diff_target: str = "HEAD") -> str:
    """Get the unified diff for changed files."""
    # Staged
    staged = subprocess.run(
        ["git", "diff", "--cached"],
        capture_output=True, text=True, cwd=repo_path,
    )
    # Unstaged
    unstaged = subprocess.run(
        ["git", "diff"],
        capture_output=True, text=True, cwd=repo_path,
    )

    diff = (staged.stdout + "\n" + unstaged.stdout).strip()

    # If no staged/unstaged, diff against previous commit
    if not diff:
        result = subprocess.run(
            ["git", "diff", f"{diff_target}~1", diff_target],
            capture_output=True, text=True, cwd=repo_path,
        )
        diff = result.stdout.strip()

    return diff


# ---------------------------------------------------------------------------
# Step 2: Extract assumptions via claude -p
# ---------------------------------------------------------------------------

def extract_assumptions(diff_content: str, verbose: bool = False) -> list[dict]:
    """Ask LLM to extract implicit assumptions from the diff.

    Returns list of {"assumption": str, "search_patterns": [str, ...]}.
    Uses claude -p (subscription CLI, $0 cost).
    """
    if not diff_content.strip():
        return []

    # Truncate very large diffs
    if len(diff_content) > 30_000:
        diff_content = diff_content[:30_000] + "\n... (truncated)"

    prompt = ASSUMPTION_PROMPT.format(diff=diff_content)

    result = subprocess.run(
        ["claude", "-p"],
        input=prompt,
        capture_output=True, text=True, timeout=120,
    )

    if result.returncode != 0:
        if verbose:
            import sys
            print(f"  [semantic] claude -p failed: {result.stderr[:200]}", file=sys.stderr)
        return []

    return _parse_assumptions(result.stdout)


def _parse_assumptions(raw: str) -> list[dict]:
    """Parse LLM response into list of assumption dicts."""
    text = raw.strip()

    # Extract JSON from markdown code block if present
    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [a for a in parsed if isinstance(a, dict) and "search_patterns" in a]
    except json.JSONDecodeError:
        pass

    # Try to find JSON array in text
    bracket_start = text.find("[")
    bracket_end = text.rfind("]")
    if bracket_start >= 0 and bracket_end > bracket_start:
        try:
            parsed = json.loads(text[bracket_start:bracket_end + 1])
            if isinstance(parsed, list):
                return [a for a in parsed if isinstance(a, dict) and "search_patterns" in a]
        except json.JSONDecodeError:
            pass

    return []


# ---------------------------------------------------------------------------
# Step 3: Search codebase for related files
# ---------------------------------------------------------------------------

def search_related_files(
    repo_path: str,
    assumptions: list[dict],
    exclude_files: set[str],
    verbose: bool = False,
) -> list[str]:
    """Grep the codebase for files matching assumption search patterns.

    Args:
        repo_path: Path to git repository root
        assumptions: List of assumption dicts with search_patterns
        exclude_files: Files already in context (skip these)

    Returns:
        List of relative file paths, deduplicated and ranked by hit count
    """
    hit_counts: dict[str, int] = {}  # path → number of pattern hits

    for assumption in assumptions:
        patterns = assumption.get("search_patterns", [])
        for pattern in patterns:
            if not pattern or len(pattern) < 3:
                continue
            try:
                # Use git grep to respect .gitignore (skips node_modules, dist, etc.)
                result = subprocess.run(
                    ["git", "grep", "-l", "-E", pattern],
                    capture_output=True, text=True, cwd=repo_path,
                    timeout=10,
                )
                for line in result.stdout.strip().split("\n"):
                    fpath = line.strip()
                    if not fpath:
                        continue
                    # Normalize: remove leading ./
                    if fpath.startswith("./"):
                        fpath = fpath[2:]
                    if fpath not in exclude_files:
                        hit_counts[fpath] = hit_counts.get(fpath, 0) + 1
            except (subprocess.TimeoutExpired, OSError):
                continue

    # Filter to source files only (reuse retrieval.py's filter)
    candidates = filter_source_files(list(hit_counts.keys()))

    # Rank by hit count (more pattern matches = more relevant)
    candidates.sort(key=lambda f: hit_counts.get(f, 0), reverse=True)

    if verbose:
        import sys
        print(f"  [semantic] {len(assumptions)} assumptions → "
              f"{len(hit_counts)} file hits → {len(candidates)} source files",
              file=sys.stderr)
        for f in candidates[:MAX_SEMANTIC_DEPS]:
            print(f"    + {f} (hits: {hit_counts.get(f, 0)})", file=sys.stderr)

    return candidates[:MAX_SEMANTIC_DEPS]


# ---------------------------------------------------------------------------
# Step 4: Expand context
# ---------------------------------------------------------------------------

def expand_context_semantic(
    repo_path: str,
    source_files: list[str],
    context: ModuleContext,
    diff_target: str = "HEAD",
    verbose: bool = False,
) -> ModuleContext:
    """Expand an existing ModuleContext with semantically related files.

    This is the main entry point, called from cli.py between Step 2 and Step 3.

    Args:
        repo_path: Path to git repository root
        source_files: List of changed source files
        context: Existing ModuleContext from build_context
        diff_target: Git ref for diff
        verbose: Print progress to stderr

    Returns:
        Updated ModuleContext with semantic deps added
    """
    import sys

    if verbose:
        print("[semantic] Extracting implicit assumptions from diff...", file=sys.stderr)

    # Step 1: Get diff
    diff_content = get_diff_content(repo_path, diff_target)
    if not diff_content:
        if verbose:
            print("  [semantic] No diff content found, skipping.", file=sys.stderr)
        return context

    # Step 2: Extract assumptions
    assumptions = extract_assumptions(diff_content, verbose=verbose)
    if not assumptions:
        if verbose:
            print("  [semantic] No assumptions extracted, skipping.", file=sys.stderr)
        return context

    if verbose:
        for i, a in enumerate(assumptions, 1):
            print(f"  [semantic] Assumption {i}: {a.get('assumption', '?')}", file=sys.stderr)
            print(f"    patterns: {a.get('search_patterns', [])}", file=sys.stderr)

    # Step 3: Search for related files
    existing_files = set(source_files)
    existing_files.update(f.path for f in context.dep_files)
    existing_files.update(f.path for f in context.target_files)

    related = search_related_files(repo_path, assumptions, existing_files, verbose=verbose)
    if not related:
        if verbose:
            print("  [semantic] No new related files found.", file=sys.stderr)
        return context

    # Step 4: Add to context
    repo = Path(repo_path)
    added = 0
    for fpath in related:
        full_path = repo / fpath
        if not full_path.exists() or not full_path.is_file():
            continue

        content = _read_file_safe(full_path)
        if content is None:
            continue

        if len(content) > MAX_FILE_CHARS:
            content = content[:MAX_FILE_CHARS] + "\n... (truncated)"

        if context.total_chars + len(content) > MAX_CONTEXT_CHARS:
            context.warnings.append(
                f"[semantic] Context limit reached, skipped remaining semantic deps"
            )
            break

        context.dep_files.append(
            FileContext(path=fpath, content=content, is_target=False)
        )
        added += 1

    if verbose:
        print(f"  [semantic] Added {added} semantic dependency file(s) to context.",
              file=sys.stderr)

    return context
