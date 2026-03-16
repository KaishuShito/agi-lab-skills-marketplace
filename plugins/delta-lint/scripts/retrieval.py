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

    elif ext == ".go":
        for m in re.finditer(r'"([^"]+)"', content):
            imports.add(m.group(1))

    elif ext == ".rs":
        for m in re.finditer(r"use\s+([\w:]+)", content):
            imports.add(m.group(1))

    elif ext == ".php":
        # require/include variants
        for m in re.finditer(r"""(?:require|include)(?:_once)?\s*[\(]?\s*['"]([^'"]+)['"]\s*[\)]?""", content):
            imports.add(m.group(1))
        # use Namespace\Class (PSR-4 style)
        for m in re.finditer(r"use\s+([\w\\]+)", content):
            imports.add(m.group(1))

    elif ext == ".rb":
        # require/require_relative
        for m in re.finditer(r"""require(?:_relative)?\s+['"]([^'"]+)['"]""", content):
            imports.add(m.group(1))

    elif ext in (".java", ".kt"):
        # import com.example.Foo
        for m in re.finditer(r"import\s+([\w.]+)", content):
            imports.add(m.group(1))

    elif ext in (".cs",):
        # using Namespace.Class
        for m in re.finditer(r"using\s+([\w.]+)\s*;", content):
            imports.add(m.group(1))

    elif ext in (".c", ".cpp", ".h", ".hpp"):
        # #include "local.h" (not <system.h>)
        for m in re.finditer(r'#include\s+"([^"]+)"', content):
            imports.add(m.group(1))

    elif ext in (".swift",):
        # import Module
        for m in re.finditer(r"import\s+(\w+)", content):
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
