"""
Retrieval layer for delta-lint MVP.

Responsible for:
1. Identifying changed files from git diff
2. Extracting imports from source files
3. Resolving import paths to actual files
4. Building context (target file + dependency files) for LLM detection

Design decisions (traced to experiment data):
- Module-level context: Experiment 1 showed Recall 45%→89% (patch→module)
- Diff-based scoping: Limits context to changed files + 1-hop deps
- v0: dependency files are included in full (with size limit)
  Future v1: extract public interfaces only
"""

import re
import subprocess
from pathlib import Path
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAX_CONTEXT_CHARS = 80_000  # ~20k tokens, well within Claude's window
MAX_FILE_CHARS = 30_000     # Skip very large files
MAX_DEPS_PER_FILE = 5       # Limit dependency fan-out


@dataclass
class FileContext:
    path: str
    content: str
    is_target: bool  # True = changed file, False = dependency


@dataclass
class ModuleContext:
    target_files: list[FileContext] = field(default_factory=list)
    dep_files: list[FileContext] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(len(f.content) for f in self.target_files + self.dep_files)

    def to_prompt_string(self) -> str:
        parts = []
        for f in self.target_files:
            parts.append(f"=== {f.path} (CHANGED) ===\n{f.content}")
        for f in self.dep_files:
            parts.append(f"=== {f.path} (DEPENDENCY) ===\n{f.content}")
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Git diff → changed files
# ---------------------------------------------------------------------------

def get_changed_files(repo_path: str, diff_target: str = "HEAD") -> list[str]:
    """Get list of changed files from git diff.

    Args:
        repo_path: Path to the git repository root
        diff_target: Git ref to diff against (default: HEAD for staged+unstaged)

    Returns:
        List of relative file paths that were changed
    """
    # Staged changes
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, cwd=repo_path,
    )
    # Unstaged changes
    unstaged = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, cwd=repo_path,
    )
    # Combine and deduplicate
    files = set()
    for output in [staged.stdout, unstaged.stdout]:
        for line in output.strip().split("\n"):
            if line.strip():
                files.add(line.strip())

    # If no staged/unstaged changes, diff against previous commit
    if not files:
        result = subprocess.run(
            ["git", "diff", f"{diff_target}~1", diff_target, "--name-only", "--diff-filter=ACMR"],
            capture_output=True, text=True, cwd=repo_path,
        )
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                files.add(line.strip())

    return sorted(files)


def filter_source_files(files: list[str]) -> list[str]:
    """Filter to source code files only (skip tests, configs, docs)."""
    source_exts = {".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx", ".py", ".go", ".rs"}
    result = []
    for f in files:
        p = Path(f)
        if p.suffix not in source_exts:
            continue
        # Skip test files
        name_lower = p.name.lower()
        if name_lower.startswith("test") or name_lower.endswith((".test.ts", ".test.js", ".spec.ts", ".spec.js", "_test.go", "_test.py")):
            continue
        # Skip __tests__ directories
        if "__tests__" in f or "__test__" in f:
            continue
        result.append(f)
    return result


# ---------------------------------------------------------------------------
# Import extraction (regex-based, v0)
# ---------------------------------------------------------------------------

def extract_imports(content: str, filename: str) -> set[str]:
    """Extract import/require paths from source code.

    Returns set of import paths (relative imports only for dependency resolution).
    Based on run_phase0_module.py's extract_imports, extended for Python.
    """
    imports = set()
    ext = Path(filename).suffix

    if ext in (".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"):
        # require('./foo') or require('../foo')
        for m in re.finditer(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""", content):
            imports.add(m.group(1))
        # import ... from './foo'
        for m in re.finditer(r"""from\s+['"]([^'"]+)['"]""", content):
            imports.add(m.group(1))
        # import './foo' (side-effect imports)
        for m in re.finditer(r"""import\s+['"]([^'"]+)['"]""", content):
            imports.add(m.group(1))

    elif ext == ".py":
        # from .module import something
        for m in re.finditer(r"from\s+(\.[.\w]*)\s+import", content):
            imports.add(m.group(1))
        # import .module (rare but possible in __init__.py)
        # Standard absolute imports are skipped (can't resolve without package info)

    elif ext == ".go":
        for m in re.finditer(r'"([^"]+)"', content):
            imports.add(m.group(1))

    elif ext == ".rs":
        for m in re.finditer(r"use\s+([\w:]+)", content):
            imports.add(m.group(1))

    # Filter to relative imports only (resolvable without package manager)
    return {i for i in imports if i.startswith(".") or i.startswith("/")}


def resolve_import_path(base_file: str, import_path: str) -> list[str]:
    """Resolve a relative import to candidate file paths.

    Returns list of candidate paths to try (first match wins).
    Based on run_phase0_module.py's resolve_import_path.
    """
    base_dir = str(Path(base_file).parent)
    resolved = str(Path(base_dir) / import_path)

    # Clean up ../ etc
    parts = resolved.split("/")
    clean = []
    for p in parts:
        if p == "..":
            if clean:
                clean.pop()
        elif p != ".":
            clean.append(p)
    resolved = "/".join(clean)

    # Try common extensions
    candidates = [resolved]
    suffix = Path(resolved).suffix
    if not suffix:
        base_ext = Path(base_file).suffix
        if base_ext in (".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"):
            for ext in [".ts", ".js", ".tsx", ".jsx", "/index.ts", "/index.js"]:
                candidates.append(resolved + ext)
        elif base_ext == ".py":
            candidates.append(resolved.replace(".", "/") + ".py")
            candidates.append(resolved.replace(".", "/") + "/__init__.py")

    return candidates


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------

def build_context(repo_path: str, changed_files: list[str]) -> ModuleContext:
    """Build module context for LLM detection.

    Args:
        repo_path: Path to the git repository root
        changed_files: List of changed file paths (relative to repo root)

    Returns:
        ModuleContext with target files and their dependencies
    """
    ctx = ModuleContext()
    repo = Path(repo_path)
    total_chars = 0
    seen_deps = set(changed_files)  # Skip deps that are already targets

    # Step 1: Read all changed files
    for fpath in changed_files:
        full_path = repo / fpath
        if not full_path.exists():
            ctx.warnings.append(f"File not found: {fpath}")
            continue

        content = _read_file_safe(full_path)
        if content is None:
            ctx.warnings.append(f"Could not read: {fpath}")
            continue

        if len(content) > MAX_FILE_CHARS:
            ctx.warnings.append(f"Truncated: {fpath} ({len(content)} chars)")
            content = content[:MAX_FILE_CHARS] + "\n... (truncated)"

        ctx.target_files.append(FileContext(path=fpath, content=content, is_target=True))
        total_chars += len(content)

    # Step 2: Resolve and read dependencies (1-hop)
    for target in ctx.target_files:
        imports = extract_imports(target.content, target.path)
        dep_count = 0

        for imp in sorted(imports):
            if dep_count >= MAX_DEPS_PER_FILE:
                break

            candidates = resolve_import_path(target.path, imp)
            for candidate in candidates:
                if candidate in seen_deps:
                    break
                full_path = repo / candidate
                if full_path.exists() and full_path.is_file():
                    content = _read_file_safe(full_path)
                    if content is None:
                        continue

                    if total_chars + len(content) > MAX_CONTEXT_CHARS:
                        ctx.warnings.append(
                            f"Context limit reached ({total_chars} chars), "
                            f"skipping remaining deps"
                        )
                        return ctx

                    if len(content) > MAX_FILE_CHARS:
                        content = content[:MAX_FILE_CHARS] + "\n... (truncated)"

                    ctx.dep_files.append(
                        FileContext(path=candidate, content=content, is_target=False)
                    )
                    seen_deps.add(candidate)
                    total_chars += len(content)
                    dep_count += 1
                    break  # Found the file, stop trying candidates

    return ctx


def _read_file_safe(path: Path) -> str | None:
    """Read a file, returning None on error."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None
