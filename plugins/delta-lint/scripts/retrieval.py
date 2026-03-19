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

import json
import os
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
    doc_files: list[FileContext] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(len(f.content)
                   for f in self.target_files + self.dep_files + self.doc_files)

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
        # Document contract surfaces — specs, ADRs, READMEs
        if self.doc_files:
            for f in self.doc_files:
                parts.append(
                    f"=== {f.path} (DOCUMENT — treat as specification contract) ===\n"
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


def _detect_base_branch(repo_path: str) -> str | None:
    """Auto-detect the base branch for PR diff.

    Priority:
      1. GITHUB_BASE_REF (set by GitHub Actions on pull_request events)
      2. Heuristic: origin/main or origin/master
    """
    env_base = os.environ.get("GITHUB_BASE_REF")
    if env_base:
        return f"origin/{env_base}"

    for candidate in ["origin/main", "origin/master"]:
        r = subprocess.run(
            ["git", "rev-parse", "--verify", candidate],
            capture_output=True, text=True, cwd=repo_path, timeout=5,
        )
        if r.returncode == 0:
            return candidate
    return None


def get_pr_changed_files(
    repo_path: str,
    base_ref: str | None = None,
) -> tuple[list[str], str]:
    """Get files changed in the entire PR (HEAD vs merge-base of base branch).

    Returns (file_list, resolved_base_ref).
    """
    if not _is_git_repo(repo_path):
        return [], ""

    if not base_ref:
        base_ref = _detect_base_branch(repo_path)
    if not base_ref:
        return [], ""

    merge_base = subprocess.run(
        ["git", "merge-base", base_ref, "HEAD"],
        capture_output=True, text=True, cwd=repo_path, timeout=10,
    )
    if merge_base.returncode != 0:
        return [], base_ref

    mb_sha = merge_base.stdout.strip()
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", mb_sha, "HEAD"],
        capture_output=True, text=True, cwd=repo_path, timeout=10,
    )
    files = sorted({
        line.strip() for line in result.stdout.strip().split("\n") if line.strip()
    })
    return files, base_ref


def get_pr_diff_content(
    repo_path: str,
    base_ref: str | None = None,
) -> str:
    """Get the full unified diff for a PR (merge-base..HEAD)."""
    if not _is_git_repo(repo_path):
        return ""
    if not base_ref:
        base_ref = _detect_base_branch(repo_path)
    if not base_ref:
        return ""
    try:
        merge_base = subprocess.run(
            ["git", "merge-base", base_ref, "HEAD"],
            capture_output=True, text=True, cwd=repo_path, timeout=10,
        )
        if merge_base.returncode != 0:
            return ""
        mb_sha = merge_base.stdout.strip()
        result = subprocess.run(
            ["git", "diff", mb_sha, "HEAD"],
            capture_output=True, text=True, cwd=repo_path, timeout=30,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


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


def get_recent_changed_files(repo_path: str, since: str = "3months") -> list[str]:
    """Get all files changed in the last N period from git log.

    Args:
        repo_path: Path to the repository root
        since: Git-compatible time spec, e.g. "3months", "6months", "1year", "90days"

    Returns:
        Sorted list of unique relative file paths changed in the period.
    """
    if not _is_git_repo(repo_path):
        return []

    # Normalize shorthand: "3months" → "3 months", "1year" → "1 year"
    import re as _re
    normalized = _re.sub(r"(\d+)(months?|years?|weeks?|days?)", r"\1 \2", since)

    result = subprocess.run(
        ["git", "log", f"--since={normalized}", "--name-only",
         "--diff-filter=ACMR", "--pretty=format:"],
        capture_output=True, text=True, cwd=repo_path, timeout=30,
    )
    files = {
        line.strip() for line in result.stdout.split("\n")
        if line.strip() and not line.startswith("commit ")
    }

    # Also include current staged/unstaged changes
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, cwd=repo_path,
    )
    unstaged = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, cwd=repo_path,
    )
    for output in [staged.stdout, unstaged.stdout]:
        for line in output.strip().split("\n"):
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
# Architecture-aware dependency detection
# ---------------------------------------------------------------------------

_ARCH_PATTERNS_CACHE: dict | None = None


def _load_arch_patterns() -> dict:
    """Load architecture_patterns.json (cached)."""
    global _ARCH_PATTERNS_CACHE
    if _ARCH_PATTERNS_CACHE is not None:
        return _ARCH_PATTERNS_CACHE
    json_path = Path(__file__).parent / "architecture_patterns.json"
    if json_path.exists():
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
            data.pop("_doc", None)
            _ARCH_PATTERNS_CACHE = data
        except (json.JSONDecodeError, OSError):
            _ARCH_PATTERNS_CACHE = {}
    else:
        _ARCH_PATTERNS_CACHE = {}
    return _ARCH_PATTERNS_CACHE


def detect_architecture(repo_path: str) -> list[str]:
    """Detect which architectures/frameworks this repo uses.

    Returns list of architecture keys (e.g., ["wordpress"], ["rails", "nextjs"]).
    Detection uses file existence and content sampling.
    """
    patterns = _load_arch_patterns()
    if not patterns:
        return []

    repo = Path(repo_path)
    detected: list[str] = []

    for arch_name, arch in patterns.items():
        score = 0

        # Check detect_files — search recursively (repos may nest frameworks
        # under subdirs like app/html/wp-content/ instead of repo root)
        for df in arch.get("detect_files", []):
            # Direct path check first (fast)
            if (repo / df).exists():
                score += 2
                break
            # Recursive: look for the leaf directory/file name anywhere
            leaf = Path(df).name or Path(df).parent.name  # handle trailing /
            if leaf:
                found = any(True for _ in repo.rglob(leaf) if not any(
                    skip in str(_) for skip in [".git", "node_modules"]
                ))
                if found:
                    score += 2
                    break

        # Check detect_content by sampling project files
        content_markers = arch.get("detect_content", [])
        if content_markers and score < 2:
            exts = arch.get("file_extensions", [])
            matches = 0
            sampled = 0
            for ext in exts:
                for p in repo.rglob(f"*{ext}"):
                    if sampled >= 15:
                        break
                    if any(skip in str(p) for skip in [
                        "node_modules", "vendor", ".git", "__pycache__",
                        "wp-admin", "wp-includes",
                    ]):
                        continue
                    try:
                        text = p.read_text(encoding="utf-8", errors="replace")[:5000]
                        for marker in content_markers:
                            if marker in text:
                                matches += 1
                                break
                    except OSError:
                        pass
                    sampled += 1
            # 2+ content matches = confident detection
            if matches >= 2:
                score += 2
            elif matches >= 1:
                score += 1

        if score >= 2:
            detected.append(arch_name)

    return detected


@dataclass
class HookDep:
    """A dependency discovered via framework hook/event pattern."""
    source_file: str      # File that emits/dispatches
    sink_file: str        # File that listens/handles
    hook_name: str        # The hook/event name connecting them
    pattern_name: str     # e.g., "action_hook", "filter_hook"
    architecture: str     # e.g., "wordpress"


def find_hook_dependencies(
    repo_path: str,
    target_files: list[str],
    architectures: list[str] | None = None,
) -> list[HookDep]:
    """Find dependencies between files via framework hooks/events.

    For each target file, finds hook names it emits (source patterns),
    then finds other files that listen on those hooks (sink patterns).
    Also finds hooks the target listens on and locates their emitters.

    Args:
        repo_path: Repository root path
        target_files: Files to find hook dependencies for
        architectures: Detected architectures (auto-detected if None)

    Returns:
        List of HookDep connections
    """
    if architectures is None:
        architectures = detect_architecture(repo_path)
    if not architectures:
        return []

    patterns = _load_arch_patterns()
    repo = Path(repo_path)
    deps: list[HookDep] = []
    seen_pairs: set[tuple[str, str, str]] = set()  # (src, sink, hook)

    for arch_name in architectures:
        arch = patterns.get(arch_name)
        if not arch:
            continue

        exts = set(arch.get("file_extensions", []))
        dep_patterns = arch.get("dependency_patterns", [])
        if not dep_patterns:
            continue

        # Build index: hook_name -> {source_files, sink_files}
        # Only index files relevant to the target set + nearby files
        hook_index: dict[str, dict[str, set[str]]] = {}
        # key = hook_name, value = {"sources": {file, ...}, "sinks": {file, ...}}

        # Collect all project files with matching extensions (excluding vendor dirs)
        project_files: list[str] = []
        for ext in exts:
            for p in repo.rglob(f"*{ext}"):
                rel = str(p.relative_to(repo))
                if any(skip in rel for skip in [
                    "node_modules", "vendor", ".git", "__pycache__",
                    "wp-admin", "wp-includes", ".delta-lint",
                ]):
                    continue
                project_files.append(rel)

        # Cap to avoid scanning massive repos
        if len(project_files) > 500:
            # Prioritize files near targets
            target_dirs = {str(Path(t).parent) for t in target_files}
            near = [f for f in project_files
                    if str(Path(f).parent) in target_dirs]
            far = [f for f in project_files
                   if str(Path(f).parent) not in target_dirs]
            project_files = near + far[:500 - len(near)]

        # Index hooks in all project files
        for fpath in project_files:
            full = repo / fpath
            try:
                content = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            for dp in dep_patterns:
                # Find source hooks (emitters)
                for m in re.finditer(dp["source"], content):
                    hook = m.group(1) if m.lastindex else m.group(0)
                    if hook not in hook_index:
                        hook_index[hook] = {"sources": set(), "sinks": set()}
                    hook_index[hook]["sources"].add(fpath)

                # Find sink hooks (listeners)
                for m in re.finditer(dp["sink"], content):
                    hook = m.group(1) if m.lastindex else m.group(0)
                    if hook not in hook_index:
                        hook_index[hook] = {"sources": set(), "sinks": set()}
                    hook_index[hook]["sinks"].add(fpath)

        # Now find connections for target files
        for target in target_files:
            full = repo / target
            if not full.exists():
                continue
            try:
                content = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            for dp in dep_patterns:
                # Target emits hook -> find sinks in other files
                for m in re.finditer(dp["source"], content):
                    hook = m.group(1) if m.lastindex else m.group(0)
                    entry = hook_index.get(hook)
                    if not entry:
                        continue
                    for sink_file in entry["sinks"]:
                        if sink_file == target:
                            continue
                        key = (target, sink_file, hook)
                        if key not in seen_pairs:
                            seen_pairs.add(key)
                            deps.append(HookDep(
                                source_file=target,
                                sink_file=sink_file,
                                hook_name=hook,
                                pattern_name=dp["name"],
                                architecture=arch_name,
                            ))

                # Target listens on hook -> find sources in other files
                for m in re.finditer(dp["sink"], content):
                    hook = m.group(1) if m.lastindex else m.group(0)
                    entry = hook_index.get(hook)
                    if not entry:
                        continue
                    for src_file in entry["sources"]:
                        if src_file == target:
                            continue
                        key = (src_file, target, hook)
                        if key not in seen_pairs:
                            seen_pairs.add(key)
                            deps.append(HookDep(
                                source_file=src_file,
                                sink_file=target,
                                hook_name=hook,
                                pattern_name=dp["name"],
                                architecture=arch_name,
                            ))

    return deps


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


def build_context(
    repo_path: str,
    changed_files: list[str],
    *,
    retrieval_config: dict | None = None,
    doc_files: list[str] | None = None,
) -> ModuleContext:
    """Build module context for LLM detection.

    Collects dependencies in tiered order:
    - Sibling (0.90): from sibling_map.yml — known sibling pairs
    - Tier 1 (same-dir, 0.95): always included
    - Tier 2 (relative, 0.85): always included
    - Tier 3 (project, 0.50): included if budget allows, capped at 2 per file

    Dependencies are sorted by confidence in the prompt so the LLM
    sees high-confidence deps first.

    Args:
        repo_path: Path to the git repository root
        changed_files: List of changed file paths (relative to repo root)
        retrieval_config: Override retrieval constants. Supported keys:
            max_context_chars, max_file_chars, max_deps_per_file, min_confidence
        doc_files: Optional list of document file paths (relative to repo root)
            to include as specification contract surfaces (README, ADR, etc.)

    Returns:
        ModuleContext with target files and their dependencies
    """
    rc = retrieval_config or {}
    _max_context = int(rc.get("max_context_chars", MAX_CONTEXT_CHARS))
    _max_file = int(rc.get("max_file_chars", MAX_FILE_CHARS))
    _max_deps = int(rc.get("max_deps_per_file", MAX_DEPS_PER_FILE))
    _min_conf = float(rc.get("min_confidence", MIN_CONFIDENCE))

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

        if len(content) > _max_file:
            ctx.warnings.append(f"Truncated: {fpath} ({len(content)} chars)")
            content = content[:_max_file] + "\n... (truncated)"

        ctx.target_files.append(FileContext(path=fpath, content=content, is_target=True))
        total_chars += len(content)

    # Step 1.5: Sibling map — add known sibling files before import deps
    try:
        from sibling import get_siblings
        sibling_files = get_siblings(repo_path, changed_files)
        for sib_path in sibling_files:
            if sib_path in seen_deps:
                continue
            full_path = repo / sib_path
            if not full_path.exists():
                continue
            content = _read_file_safe(full_path)
            if content is None:
                continue
            if total_chars + len(content) > _max_context:
                ctx.warnings.append(
                    f"Context limit reached ({total_chars} chars), "
                    f"skipping remaining sibling deps"
                )
                break
            if len(content) > _max_file:
                content = content[:_max_file] + "\n... (truncated)"
            ctx.dep_files.append(FileContext(
                path=sib_path,
                content=content,
                is_target=False,
                confidence=0.90,
                dep_tier="sibling-map",
            ))
            seen_deps.add(sib_path)
            total_chars += len(content)
    except ImportError:
        pass  # sibling module not available — skip silently

    # Step 2: Resolve and read dependencies (1-hop) with tiered confidence
    for target in ctx.target_files:
        imports = extract_imports(target.content, target.path)
        dep_count = 0
        tier3_count = 0  # Cap Tier 3 deps to avoid noise
        MAX_TIER3_PER_FILE = 2

        # Sort: relative imports first (higher tier), then non-relative
        sorted_imports = sorted(imports, key=lambda i: (not i.is_relative, i.path))

        for imp in sorted_imports:
            if dep_count >= _max_deps:
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
            if tier.confidence < _min_conf:
                continue

            if total_chars + len(content) > _max_context:
                ctx.warnings.append(
                    f"Context limit reached ({total_chars} chars), "
                    f"skipping remaining deps"
                )
                return ctx

            if len(content) > _max_file:
                content = content[:_max_file] + "\n... (truncated)"

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

    # Step 3: Architecture-aware hook dependencies
    # Discovers connections via framework hooks/events (WordPress actions,
    # Django signals, Rails callbacks, etc.) that import analysis misses.
    try:
        archs = detect_architecture(repo_path)
        if archs:
            hook_deps = find_hook_dependencies(
                repo_path, changed_files, architectures=archs,
            )
            for hd in hook_deps:
                dep_file = hd.sink_file if hd.source_file in changed_files else hd.source_file
                if dep_file in seen_deps:
                    continue

                full_path = repo / dep_file
                content = _read_file_safe(full_path)
                if content is None:
                    continue

                if total_chars + len(content) > _max_context:
                    ctx.warnings.append(
                        f"Context limit reached ({total_chars} chars), "
                        f"skipping remaining hook deps"
                    )
                    break

                if len(content) > _max_file:
                    content = content[:_max_file] + "\n... (truncated)"

                ctx.dep_files.append(FileContext(
                    path=dep_file,
                    content=content,
                    is_target=False,
                    confidence=0.80,  # Between Tier 2 (0.85) and Tier 3 (0.50)
                    dep_tier=f"hook:{hd.architecture}/{hd.pattern_name}({hd.hook_name})",
                ))
                seen_deps.add(dep_file)
                total_chars += len(content)
    except Exception:
        pass  # Hook detection is best-effort, never block the scan

    # Step 4: Document contract surfaces (README, ADR, specs)
    # These are treated as specification contracts — the LLM checks whether
    # the code contradicts what the documentation promises.
    if doc_files:
        for doc_path in doc_files:
            full_path = repo / doc_path
            if not full_path.exists():
                ctx.warnings.append(f"Document not found: {doc_path}")
                continue
            content = _read_file_safe(full_path)
            if content is None:
                ctx.warnings.append(f"Could not read document: {doc_path}")
                continue
            if total_chars + len(content) > _max_context:
                ctx.warnings.append(
                    f"Context limit reached ({total_chars} chars), "
                    f"skipping remaining documents"
                )
                break
            if len(content) > _max_file:
                content = content[:_max_file] + "\n... (truncated)"
            ctx.doc_files.append(FileContext(
                path=doc_path,
                content=content,
                is_target=False,
                confidence=1.0,
                dep_tier="document",
            ))
            total_chars += len(content)

    return ctx


def get_diff_content(repo_path: str, diff_target: str = "HEAD") -> str:
    """Get git diff text for change-aware detection."""
    if not _is_git_repo(repo_path):
        return ""
    try:
        result = subprocess.run(
            ["git", "diff", diff_target],
            capture_output=True, text=True, cwd=repo_path, timeout=10,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def _read_file_safe(path: Path) -> str | None:
    """Read a file, returning None on error."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Smart file selection (git history-based prioritization)
# ---------------------------------------------------------------------------

def get_priority_files(
    repo_path: str,
    *,
    months: int = 24,
    max_files: int = 30,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> list[str]:
    """Select high-priority files for scanning based on git history.

    Scoring: churn × recency × fan_out_bonus
    - churn: number of commits touching the file
    - recency: recent changes weighted higher (exponential decay)
    - fan_out_bonus: files referenced by structure.json hotspots get 2× boost

    Returns list of relative file paths, sorted by priority score,
    fitting within max_chars budget.
    """
    import json
    from collections import defaultdict
    from math import exp

    repo = Path(repo_path)
    if not _is_git_repo(repo_path):
        return []

    # Step 1: Get per-file churn + recency from git log
    result = subprocess.run(
        ["git", "log", f"--since={months} months ago", "--format=%at", "--name-only"],
        capture_output=True, text=True, cwd=repo_path,
    )

    file_churn: dict[str, int] = defaultdict(int)
    file_latest_ts: dict[str, float] = {}
    current_ts = 0.0

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.isdigit():
            current_ts = float(line)
        else:
            file_churn[line] += 1
            if line not in file_latest_ts or current_ts > file_latest_ts[line]:
                file_latest_ts[line] = current_ts

    if not file_churn:
        return []

    # Step 2: Filter to source files that still exist
    source_files = filter_source_files(list(file_churn.keys()))
    source_files = [f for f in source_files if (repo / f).exists()]

    # Step 3: Load hotspots + fan-out from structure.json if available
    hotspot_paths: set[str] = set()
    fan_out_map: dict[str, int] = {}
    structure_path = repo / ".delta-lint" / "stress-test" / "structure.json"
    if structure_path.exists():
        try:
            struct = json.loads(structure_path.read_text(encoding="utf-8"))
            for h in struct.get("hotspots", []):
                hotspot_paths.add(h.get("path", ""))
            # Count how many modules depend on each file
            dep_count: dict[str, int] = defaultdict(int)
            for m in struct.get("modules", []):
                for dep in m.get("dependencies", []):
                    dep_count[dep] += 1
            fan_out_map = dict(dep_count)
        except (json.JSONDecodeError, KeyError):
            pass

    # Step 4: Score each file
    import time
    now = time.time()
    max_churn = max(file_churn.values()) if file_churn else 1

    scored: list[tuple[float, str]] = []
    for f in source_files:
        churn = file_churn.get(f, 0)
        churn_norm = churn / max_churn  # 0..1

        # Recency: exponential decay, half-life = 3 months
        age_months = (now - file_latest_ts.get(f, 0)) / (30 * 86400)
        recency = exp(-0.23 * age_months)  # 0.23 ≈ ln(2)/3

        # Fan-out bonus
        fan_bonus = 1.0
        if f in hotspot_paths:
            fan_bonus = 3.0
        elif fan_out_map.get(f, 0) >= 3:
            fan_bonus = 2.0
        elif fan_out_map.get(f, 0) >= 1:
            fan_bonus = 1.5

        score = churn_norm * recency * fan_bonus
        scored.append((score, f))

    scored.sort(reverse=True)

    # Step 5: Pack into context budget using multi-batch strategy
    # High-churn files are often large. We return a flat list but
    # callers can split into batches by checking sizes.
    selected: list[str] = []
    total_chars = 0
    for _score, f in scored:
        if len(selected) >= max_files:
            break
        fpath = repo / f
        try:
            size = fpath.stat().st_size
        except OSError:
            continue
        # Skip tiny files (< 200 bytes) — CSS/JS stubs, empty wrappers
        if size < 200:
            continue
        # Skip extremely large files (> 100KB)
        if size > 100_000:
            continue
        if total_chars + size > max_chars:
            continue
        selected.append(f)
        total_chars += size

    return selected


def get_priority_batches(
    repo_path: str,
    *,
    months: int = 24,
    max_batch_chars: int = MAX_CONTEXT_CHARS,
) -> list[list[str]]:
    """Split priority files into multiple batches for parallel scanning.

    Large high-churn files get their own batch (solo scan).
    Remaining files are packed greedily into batches.
    Returns list of batches, each a list of file paths.
    """
    import json
    from collections import defaultdict
    from math import exp
    import time

    repo = Path(repo_path)
    if not _is_git_repo(repo_path):
        return []

    # Reuse scoring logic from get_priority_files but with all files
    result = subprocess.run(
        ["git", "log", f"--since={months} months ago", "--format=%at", "--name-only"],
        capture_output=True, text=True, cwd=repo_path,
    )

    file_churn: dict[str, int] = defaultdict(int)
    file_latest_ts: dict[str, float] = {}
    current_ts = 0.0

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.isdigit():
            current_ts = float(line)
        else:
            file_churn[line] += 1
            if line not in file_latest_ts or current_ts > file_latest_ts[line]:
                file_latest_ts[line] = current_ts

    if not file_churn:
        return []

    source_files = filter_source_files(list(file_churn.keys()))
    source_files = [f for f in source_files if (repo / f).exists()]

    # Load hotspots
    hotspot_paths: set[str] = set()
    fan_out_map: dict[str, int] = {}
    structure_path = repo / ".delta-lint" / "stress-test" / "structure.json"
    if structure_path.exists():
        try:
            struct = json.loads(structure_path.read_text(encoding="utf-8"))
            for h in struct.get("hotspots", []):
                hotspot_paths.add(h.get("path", ""))
            dep_count: dict[str, int] = defaultdict(int)
            for m in struct.get("modules", []):
                for dep in m.get("dependencies", []):
                    dep_count[dep] += 1
            fan_out_map = dict(dep_count)
        except (json.JSONDecodeError, KeyError):
            pass

    now = time.time()
    max_churn = max(file_churn.values()) if file_churn else 1

    scored: list[tuple[float, str, int]] = []  # (score, path, size)
    for f in source_files:
        fpath = repo / f
        try:
            size = fpath.stat().st_size
        except OSError:
            continue
        if size < 200 or size > 100_000:
            if size > 100_000:
                # Large files still considered — will be solo batched
                pass
            else:
                continue

        churn_norm = file_churn.get(f, 0) / max_churn
        age_months = (now - file_latest_ts.get(f, 0)) / (30 * 86400)
        recency = exp(-0.23 * age_months)

        fan_bonus = 1.0
        if f in hotspot_paths:
            fan_bonus = 3.0
        elif fan_out_map.get(f, 0) >= 3:
            fan_bonus = 2.0
        elif fan_out_map.get(f, 0) >= 1:
            fan_bonus = 1.5

        score = churn_norm * recency * fan_bonus
        scored.append((score, f, size))

    scored.sort(reverse=True)

    # Split into batches: large files solo, small files packed
    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_size = 0

    for _score, f, size in scored[:100]:  # Top 100 files max
        if size > max_batch_chars // 2:
            # Large file → solo batch
            batches.append([f])
        else:
            if current_size + size > max_batch_chars:
                if current_batch:
                    batches.append(current_batch)
                current_batch = [f]
                current_size = size
            else:
                current_batch.append(f)
                current_size += size

    if current_batch:
        batches.append(current_batch)

    return batches


def _pack_batches(
    repo_path: str,
    files: list[str],
    *,
    max_batch_chars: int = MAX_CONTEXT_CHARS,
) -> list[list[str]]:
    """Pack files into batches by size.

    Simple greedy packing: large files get their own batch,
    smaller files are packed until max_batch_chars is reached.
    Used by --scope wide to batch-split all source files.
    """
    repo = Path(repo_path)
    sized: list[tuple[str, int]] = []
    for f in files:
        fpath = repo / f
        try:
            size = fpath.stat().st_size
        except OSError:
            continue
        if size < 200:
            continue
        sized.append((f, size))

    # Sort by size descending so large files are placed first
    sized.sort(key=lambda x: x[1], reverse=True)

    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_size = 0

    for f, size in sized:
        if size > max_batch_chars // 2:
            batches.append([f])
        else:
            if current_size + size > max_batch_chars:
                if current_batch:
                    batches.append(current_batch)
                current_batch = [f]
                current_size = size
            else:
                current_batch.append(f)
                current_size += size

    if current_batch:
        batches.append(current_batch)

    return batches
