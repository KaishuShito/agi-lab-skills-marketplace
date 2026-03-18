"""
Scan result cache for delta-lint.

Caches detection results keyed by a hash of the context (file paths + content).
If the same set of files with the same content is scanned again, cached findings
are returned instantly without calling the LLM.

Cache location: .delta-lint/cache/
"""

import hashlib
import json
from pathlib import Path
from datetime import datetime


def _cache_dir(repo_path: str) -> Path:
    return Path(repo_path) / ".delta-lint" / "cache"


def compute_context_hash(target_files: list, dep_files: list) -> str:
    """Compute a hash of the scan context (file paths + content).

    Args:
        target_files: list of FileContext (or dicts with path, content)
        dep_files: list of FileContext (or dicts with path, content)

    Returns:
        SHA256 hex digest (first 16 chars)
    """
    parts = []
    for f in sorted(target_files, key=lambda x: _get_path(x)):
        parts.append(f"{_get_path(f)}:{hashlib.md5(_get_content(f).encode()).hexdigest()}")
    for f in sorted(dep_files, key=lambda x: _get_path(x)):
        parts.append(f"dep:{_get_path(f)}:{hashlib.md5(_get_content(f).encode()).hexdigest()}")
    combined = "|".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


def _get_path(f) -> str:
    if isinstance(f, dict):
        return f.get("path", "")
    return getattr(f, "path", "")


def _get_content(f) -> str:
    if isinstance(f, dict):
        return f.get("content", "")
    return getattr(f, "content", "")


def get_cached_findings(repo_path: str, context_hash: str) -> list[dict] | None:
    """Look up cached findings for a context hash.

    Returns None if no cache hit (or cache expired/corrupt).
    """
    cache_file = _cache_dir(repo_path) / f"{context_hash}.json"
    if not cache_file.exists():
        return None

    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        return data.get("findings")
    except (json.JSONDecodeError, OSError):
        return None


def save_cached_findings(
    repo_path: str,
    context_hash: str,
    findings: list[dict],
    model: str = "",
) -> Path:
    """Save findings to cache."""
    cache_dir = _cache_dir(repo_path)
    cache_dir.mkdir(parents=True, exist_ok=True)

    cache_file = cache_dir / f"{context_hash}.json"
    data = {
        "context_hash": context_hash,
        "timestamp": datetime.now().isoformat(),
        "model": model,
        "findings_count": len(findings),
        "findings": findings,
    }
    cache_file.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return cache_file


def clear_cache(repo_path: str) -> int:
    """Remove all cached scan results. Returns number of files removed."""
    cache_dir = _cache_dir(repo_path)
    if not cache_dir.exists():
        return 0
    count = 0
    for f in cache_dir.glob("*.json"):
        f.unlink()
        count += 1
    return count
