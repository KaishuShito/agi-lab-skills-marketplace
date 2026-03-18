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

Tiered confidence (inspired by GitNexus resolution-context.ts):
- Tier 1 (0.95): same-directory explicit import
- Tier 2 (0.85): relative import resolved to file (cross-directory)
- Tier 3 (0.50): project-scope name match (non-relative import)
- Dependencies below MIN_CONFIDENCE are excluded from LLM context
"""

import re
import subprocess
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAX_CONTEXT_CHARS = 80_000  # ~20k tokens, well within Claude's window
MAX_FILE_CHARS = 30_000     # Skip very large files
MAX_DEPS_PER_FILE = 5       # Limit dependency fan-out
MIN_CONFIDENCE = 0.50       # Dependencies below this are excluded


class DepTier(Enum):
    """Dependency resolution confidence tier."""
    SAME_DIR = 1     # Same directory, explicit import → confidence 0.95
    RELATIVE = 2     # Relative import, cross-directory → confidence 0.85
    PROJECT = 3      # Project-scope name match → confidence 0.50

    @property
    def confidence(self) -> float:
        return {
            DepTier.SAME_DIR: 0.95,
            DepTier.RELATIVE: 0.85,
            DepTier.PROJECT: 0.50,
        }[self]

    @property
    def label(self) -> str:
        return {
            DepTier.SAME_DIR: "same-dir import",
            DepTier.RELATIVE: "relative import",
            DepTier.PROJECT: "project-scope match",
        }[self]


@dataclass
class FileContext:
    path: str
    content: str
    is_target: bool  # True = changed file, False = dependency
    confidence: float = 1.0  # 1.0 for targets, tier-based for deps
    dep_tier: str = ""  # DepTier label, empty for targets


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
        # Sort deps by confidence descending — LLM sees high-confidence deps first
        sorted_deps = sorted(self.dep_files, key=lambda d: -d.confidence)
        for f in sorted_deps:
            conf_pct = int(f.confidence * 100)
            parts.append(
                f"=== {f.path} (DEPENDENCY, confidence={conf_pct}%, {f.dep_tier}) ===\n"
                f"{f.content}"
            )
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Git diff → changed files
# ---------------------------------------------------------------------------

def _is_git_repo(path: str) -> bool:
    """Check if path is inside a git repository."""
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, cwd=path, timeout=5,
    )
    return result.returncode == 0


def get_changed_files(repo_path: str, diff_target: str = "HEAD") -> list[str]:
    """Get list of changed files from git diff.

    If not a git repo, returns empty list (caller should use --files instead).

    Args:
        repo_path: Path to the repository root
        diff_target: Git ref to diff against (default: HEAD for staged+unstaged)

    Returns:
        List of relative file paths that were changed
    """
    if not _is_git_repo(repo_path):
        return []

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
    """Filter to source code files using exclude-list approach.

    Instead of an allow-list of extensions, exclude known non-source files.
    This makes delta-lint language-agnostic (PHP, Ruby, Java, C#, etc.).
    """
    # Extensions to exclude (binary, data, config, docs, assets)
    exclude_exts = {
        # Binary / compiled
        ".exe", ".dll", ".so", ".dylib", ".o", ".a", ".pyc", ".pyo",
        ".class", ".jar", ".war", ".wasm", ".bin",
        # Images / media
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp",
        ".mp3", ".mp4", ".wav", ".avi", ".mov", ".webm",
        # Fonts
        ".woff", ".woff2", ".ttf", ".eot", ".otf",
        # Data / serialization
        ".json", ".yaml", ".yml", ".toml", ".xml", ".csv", ".tsv",
        ".sql", ".sqlite", ".db",
        # Docs / text
        ".md", ".txt", ".rst", ".pdf", ".doc", ".docx",
        # Config / build
        ".lock", ".sum", ".mod",
        ".env", ".ini", ".cfg", ".conf",
        ".dockerignore", ".gitignore", ".editorconfig",
        # Archives
        ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
        # Maps / generated
        ".map", ".min.js", ".min.css",
        # Certificates / keys
        ".pem", ".crt", ".key", ".p12",
        # Translation / i18n
        ".po", ".mo", ".pot",
        # Spreadsheet / office
        ".xlsx", ".xls", ".pptx",
        # Other non-source
        ".heic", ".fig", ".cache", ".meta",
    }

    # Directory patterns to exclude (framework core, 3rd party, build artifacts)
    # Reference: https://github.com/karesansui-u/agi-lab-skills-marketplace
    exclude_dirs = {
        # Version control
        ".git",
        # Package managers / dependencies
        "node_modules", "vendor", "bower_components", "jspm_packages",
        ".yarn", "packages", ".gem",
        # CMS core (don't modify)
        "wp-admin", "wp-includes", "wp-snapshots",  # WordPress
        "core",                                       # Drupal 8+
        "administrator",                              # Joomla
        "typo3", "typo3_src", "fileadmin",            # TYPO3
        # PHP frameworks
        "storage", "bootstrap",              # Laravel
        "var",                               # Symfony
        "tmp",                               # CakePHP
        "system",                            # CodeIgniter
        "webroot",                           # CakePHP generated
        # Python
        "__pycache__", ".venv", "venv", "env", "site-packages",
        ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        "staticfiles", "htmlcov", "migrations",
        # Ruby / Rails
        ".bundle", "bundle", "gems", "log",
        "public/assets", "public/packs",     # Rails asset pipeline / Webpacker
        "sorbet",                            # Sorbet RBI files
        # Java / Kotlin / JVM
        "target", "build", ".gradle", ".mvn", ".m2", "out",
        ".idea", "classes", "test-classes",
        "generated-sources", "generated-test-sources",
        # Android
        ".cxx", "intermediates", "outputs", "transforms",
        "GeneratedPluginRegistrant",
        # iOS / macOS
        "DerivedData", "Pods", "Carthage", ".build",
        "xcuserdata", "xcshareddata", "SourcePackages", "checkouts",
        # .NET
        "bin", "obj", ".nuget", "TestResults", "publish",
        "BenchmarkDotNet.Artifacts", "_ReSharper", ".vs", "AppPackages",
        # Frontend build
        "dist", ".next", ".nuxt", ".output",  # .output = Nuxt 3
        ".svelte-kit", ".vite", ".angular",
        ".parcel-cache", ".cache", ".turbo", ".webpack",
        "storybook-static", ".docusaurus", ".gatsby",
        ".nyc_output",                       # Istanbul/NYC coverage
        # Go
        "pkg",
        # Rust / Cargo
        ".cargo", "registry", "debug", "release", "deps", "incremental",
        # Infrastructure / IaC
        ".terraform", ".terraform.d", ".pulumi", "cdk.out",
        # Container / VM
        ".vagrant", ".docker",
        # Test / coverage artifacts
        "coverage", "lcov-report", "__snapshots__", "test-results",
        # Documentation artifacts
        "_site", "site", "_build", "docs-dist", "api-docs",
        # Unity
        "Library", "Temp", "Obj", "Logs", "UserSettings", "MemoryCaptures",
        # Unreal Engine
        "Binaries", "Intermediate", "Saved", "DerivedDataCache",
        # Flutter
        "Flutter",
        # Code generation
        "generated", "proto-gen", "openapi-gen", "__generated__",
        # Misc tool caches
        ".eslintcache", ".stylelintcache", ".nx",
        "cache", "logs",
        # Assets (non-code)
        "assets", "static", "public/uploads",
    }

    # Test file patterns
    test_patterns = {
        ".test.ts", ".test.js", ".test.tsx", ".test.jsx",
        ".spec.ts", ".spec.js", ".spec.tsx", ".spec.jsx",
        "_test.go", "_test.py", "_test.rb",
        "Test.java", "Test.kt", "Test.cs",
    }

    result = []
    for f in files:
        p = Path(f)

        # Skip by extension
        if p.suffix.lower() in exclude_exts:
            continue

        # Skip files with no extension (Makefile, Dockerfile are ok)
        if not p.suffix and p.name not in {"Makefile", "Dockerfile", "Rakefile", "Gemfile"}:
            continue

        # Skip excluded directories
        parts = set(p.parts)
        if parts & exclude_dirs:
            continue

        # Skip test files
        name_lower = p.name.lower()
        if name_lower.startswith("test_") or name_lower.startswith("test."):
            continue
        if any(name_lower.endswith(pat.lower()) for pat in test_patterns):
            continue
        if "__tests__" in f or "__test__" in f or "/tests/" in f or "/test/" in f:
            continue

        result.append(f)
    return result


# ---------------------------------------------------------------------------
# Import extraction (regex-based, v0)
# ---------------------------------------------------------------------------

@dataclass
class ImportInfo:
    """An extracted import with its resolution tier hint."""
    path: str
    is_relative: bool  # True = ./foo, ../bar, .module — resolvable by path
    # Named symbols imported (e.g., {"User", "validate"} from "from .auth import User, validate")
    symbols: frozenset[str] = frozenset()


def extract_imports(content: str, filename: str) -> list[ImportInfo]:
    """Extract import/require paths from source code.

    Returns list of ImportInfo with relative/non-relative classification.
    Non-relative imports are kept for Tier 3 project-scope resolution.
    """
    relative: list[ImportInfo] = []
    nonrelative: list[ImportInfo] = []
    ext = Path(filename).suffix

    if ext in (".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"):
        # require('./foo') or require('../foo')
        for m in re.finditer(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""", content):
            p = m.group(1)
            (relative if p.startswith(".") else nonrelative).append(
                ImportInfo(path=p, is_relative=p.startswith(".")))
        # import { X, Y } from './foo'  or  import Foo from './foo'
        for m in re.finditer(
            r"""(?:import\s+(?:\{([^}]+)\}|(\w+))\s+from|from)\s+['"]([^'"]+)['"]""",
            content
        ):
            syms_bracket, sym_default, p = m.group(1), m.group(2), m.group(3)
            symbols: set[str] = set()
            if syms_bracket:
                for s in syms_bracket.split(","):
                    name = s.strip().split(" as ")[0].strip()
                    if name:
                        symbols.add(name)
            if sym_default:
                symbols.add(sym_default)
            is_rel = p.startswith(".")
            (relative if is_rel else nonrelative).append(
                ImportInfo(path=p, is_relative=is_rel, symbols=frozenset(symbols)))
        # import './foo' (side-effect imports)
        for m in re.finditer(r"""import\s+['"]([^'"]+)['"]""", content):
            p = m.group(1)
            (relative if p.startswith(".") else nonrelative).append(
                ImportInfo(path=p, is_relative=p.startswith(".")))

    elif ext == ".py":
        # from .module import something, other
        for m in re.finditer(r"from\s+(\.[.\w]*)\s+import\s+([^\n;]+)", content):
            mod_path = m.group(1)
            names = {n.strip().split(" as ")[0].strip()
                     for n in m.group(2).split(",") if n.strip()}
            relative.append(ImportInfo(
                path=mod_path, is_relative=True, symbols=frozenset(names)))
        # from module import something (non-relative)
        for m in re.finditer(r"from\s+([a-zA-Z_]\w*(?:\.\w+)*)\s+import\s+([^\n;]+)", content):
            mod_path = m.group(1)
            names = {n.strip().split(" as ")[0].strip()
                     for n in m.group(2).split(",") if n.strip()}
            nonrelative.append(ImportInfo(
                path=mod_path, is_relative=False, symbols=frozenset(names)))
        # import module (bare import, non-relative)
        for m in re.finditer(r"^import\s+([a-zA-Z_]\w*(?:\.\w+)*)\s*$", content, re.MULTILINE):
            nonrelative.append(ImportInfo(path=m.group(1), is_relative=False))

    elif ext == ".go":
        # Go imports: only keep project-internal paths (contain no dots = likely stdlib)
        for m in re.finditer(r'"([^"]+)"', content):
            p = m.group(1)
            nonrelative.append(ImportInfo(path=p, is_relative=False))

    elif ext == ".rs":
        for m in re.finditer(r"use\s+([\w:]+)", content):
            p = m.group(1)
            is_rel = p.startswith("crate::") or p.startswith("super::")
            (relative if is_rel else nonrelative).append(
                ImportInfo(path=p, is_relative=is_rel))

    elif ext == ".php":
        for m in re.finditer(r"""(?:require|include)(?:_once)?\s*[\(]?\s*['"]([^'"]+)['"]\s*[\)]?""", content):
            p = m.group(1)
            relative.append(ImportInfo(path=p, is_relative=True))
        for m in re.finditer(r"use\s+([\w\\]+)", content):
            nonrelative.append(ImportInfo(path=m.group(1), is_relative=False))

    elif ext == ".rb":
        for m in re.finditer(r"""require_relative\s+['"]([^'"]+)['"]""", content):
            relative.append(ImportInfo(path=m.group(1), is_relative=True))
        for m in re.finditer(r"""require\s+['"]([^'"]+)['"]""", content):
            nonrelative.append(ImportInfo(path=m.group(1), is_relative=False))

    elif ext in (".java", ".kt"):
        for m in re.finditer(r"import\s+([\w.]+)", content):
            nonrelative.append(ImportInfo(path=m.group(1), is_relative=False))

    elif ext in (".cs",):
        for m in re.finditer(r"using\s+([\w.]+)\s*;", content):
            nonrelative.append(ImportInfo(path=m.group(1), is_relative=False))

    elif ext in (".c", ".cpp", ".h", ".hpp"):
        for m in re.finditer(r'#include\s+"([^"]+)"', content):
            relative.append(ImportInfo(path=m.group(1), is_relative=True))

    elif ext in (".swift",):
        for m in re.finditer(r"import\s+(\w+)", content):
            nonrelative.append(ImportInfo(path=m.group(1), is_relative=False))

    # Deduplicate by path (keep first occurrence with most symbol info)
    seen: dict[str, ImportInfo] = {}
    for imp in relative + nonrelative:
        if imp.path not in seen or len(imp.symbols) > len(seen[imp.path].symbols):
            seen[imp.path] = imp
    return list(seen.values())


@dataclass
class ResolvedDep:
    """A resolved dependency with its tier and candidate paths."""
    import_info: ImportInfo
    tier: DepTier
    candidates: list[str]  # File paths to try (first match wins)


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


def _classify_tier(base_file: str, imp: ImportInfo, resolved_path: str) -> DepTier:
    """Classify the dependency tier based on import type and location."""
    if not imp.is_relative:
        return DepTier.PROJECT  # Tier 3: non-relative import

    # Relative import — check if same directory
    base_dir = str(Path(base_file).parent)
    resolved_dir = str(Path(resolved_path).parent)
    if base_dir == resolved_dir:
        return DepTier.SAME_DIR  # Tier 1: same directory
    return DepTier.RELATIVE  # Tier 2: cross-directory relative


def resolve_import_tiered(base_file: str, imp: ImportInfo,
                          repo_path: str) -> ResolvedDep:
    """Resolve an import with tier classification.

    For relative imports: use standard path resolution.
    For non-relative imports: search project files by last segment name match.
    """
    if imp.is_relative:
        candidates = resolve_import_path(base_file, imp.path)
        # Tier is determined after we find the actual file
        return ResolvedDep(import_info=imp, tier=DepTier.RELATIVE, candidates=candidates)

    # Non-relative: project-scope search (Tier 3)
    # Convert module path to filename candidates
    # e.g., "auth.session" → ["auth/session.py", "auth/session.ts", ...]
    # e.g., "com.example.UserService" → ["UserService.java", "UserService.kt"]
    candidates = _nonrelative_candidates(imp.path, base_file, repo_path)
    return ResolvedDep(import_info=imp, tier=DepTier.PROJECT, candidates=candidates)


def _nonrelative_candidates(import_path: str, base_file: str,
                            repo_path: str) -> list[str]:
    """Generate candidate file paths for a non-relative import.

    Uses the last segment of the import path as filename to search for.
    """
    ext = Path(base_file).suffix
    candidates = []

    if ext == ".py":
        # "auth.session" → "auth/session.py"
        parts = import_path.split(".")
        rel = "/".join(parts)
        candidates.append(rel + ".py")
        candidates.append(rel + "/__init__.py")
        # Also try just the last part in common locations
        last = parts[-1]
        candidates.append(last + ".py")

    elif ext in (".java", ".kt"):
        # "com.example.UserService" → search for UserService.java
        last = import_path.rsplit(".", 1)[-1]
        candidates.append(last + ext)

    elif ext in (".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"):
        # "@scope/package" or "lodash" → skip (node_modules)
        # But "src/utils" → try resolving
        if import_path.startswith("@") or "/" not in import_path:
            return []  # npm package, skip
        for e in [".ts", ".js", ".tsx", ".jsx"]:
            candidates.append(import_path + e)

    elif ext == ".go":
        # Go: skip stdlib (no dots or starts with standard prefixes)
        # Only keep project-internal (contains the repo's module path)
        if "/" not in import_path or import_path.count("/") < 2:
            return []  # likely stdlib
        last_segment = import_path.rsplit("/", 1)[-1]
        candidates.append(last_segment + ".go")

    return candidates


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------

def _find_project_file(repo: Path, candidates: list[str],
                       seen: set[str]) -> tuple[str, Path] | None:
    """Try candidates in order. Also search recursively for bare filenames."""
    for candidate in candidates:
        if candidate in seen:
            return None
        full = repo / candidate
        if full.exists() and full.is_file():
            return candidate, full

    # For bare filenames (e.g., "UserService.java"), search project tree
    for candidate in candidates:
        if "/" not in candidate and candidate not in seen:
            # Shallow search: walk top 3 directory levels
            for depth_limit_dir in repo.rglob(candidate):
                if depth_limit_dir.is_file():
                    rel = str(depth_limit_dir.relative_to(repo))
                    if rel not in seen:
                        return rel, depth_limit_dir
    return None


def build_context(repo_path: str, changed_files: list[str]) -> ModuleContext:
    """Build module context for LLM detection.

    Collects dependencies in tiered order:
    - Tier 1 (same-dir, 0.95): always included
    - Tier 2 (relative, 0.85): always included
    - Tier 3 (project, 0.50): included if budget allows, capped at 2 per file

    Dependencies are sorted by confidence in the prompt so the LLM
    sees high-confidence deps first.

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

    # Step 2: Resolve and read dependencies (1-hop) with tiered confidence
    for target in ctx.target_files:
        imports = extract_imports(target.content, target.path)
        dep_count = 0
        tier3_count = 0  # Cap Tier 3 deps to avoid noise
        MAX_TIER3_PER_FILE = 2

        # Sort: relative imports first (higher tier), then non-relative
        sorted_imports = sorted(imports, key=lambda i: (not i.is_relative, i.path))

        for imp in sorted_imports:
            if dep_count >= MAX_DEPS_PER_FILE:
                break

            # Skip Tier 3 if budget exhausted
            if not imp.is_relative and tier3_count >= MAX_TIER3_PER_FILE:
                continue

            resolved = resolve_import_tiered(target.path, imp, repo_path)
            found = _find_project_file(repo, resolved.candidates, seen_deps)
            if found is None:
                continue

            candidate, full_path = found
            content = _read_file_safe(full_path)
            if content is None:
                continue

            # Determine actual tier now that we know the resolved path
            tier = _classify_tier(target.path, imp, candidate)

            # Skip below minimum confidence
            if tier.confidence < MIN_CONFIDENCE:
                continue

            if total_chars + len(content) > MAX_CONTEXT_CHARS:
                ctx.warnings.append(
                    f"Context limit reached ({total_chars} chars), "
                    f"skipping remaining deps"
                )
                return ctx

            if len(content) > MAX_FILE_CHARS:
                content = content[:MAX_FILE_CHARS] + "\n... (truncated)"

            ctx.dep_files.append(FileContext(
                path=candidate,
                content=content,
                is_target=False,
                confidence=tier.confidence,
                dep_tier=tier.label,
            ))
            seen_deps.add(candidate)
            total_chars += len(content)
            dep_count += 1
            if tier == DepTier.PROJECT:
                tier3_count += 1

    return ctx


def _read_file_safe(path: Path) -> str | None:
    """Read a file, returning None on error."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None
