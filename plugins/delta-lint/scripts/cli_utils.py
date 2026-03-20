"""Shared utility functions for delta-lint CLI commands."""

import argparse
import json
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Adaptive time window
# ---------------------------------------------------------------------------

def _adaptive_since(repo_path: str, verbose: bool = False) -> str:
    """Determine scan time window based on commit frequency."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", "--since=3 months", "--oneline"],
            capture_output=True, text=True, cwd=repo_path, timeout=10,
        )
        commit_count = len([l for l in result.stdout.strip().split("\n") if l.strip()])
    except Exception:
        return "3months"

    if commit_count >= 30:
        since = "3months"
    elif commit_count >= 10:
        since = "6months"
    elif commit_count >= 3:
        since = "9months"
    else:
        since = "1year"

    if verbose or since != "3months":
        print(f"  🔍 Adaptive window: {commit_count} commits in 3 months → since={since}", file=sys.stderr)
    return since


# ---------------------------------------------------------------------------
# Dashboard auto-open helper
# ---------------------------------------------------------------------------

def _is_ci() -> bool:
    """Detect CI environment."""
    return bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS")
                or os.environ.get("GITLAB_CI") or os.environ.get("JENKINS_URL"))


def _open_dashboard(dash_path: str, *, force: bool = False, live: bool = False) -> bool:
    """Open dashboard HTML file in browser if not CI. Returns True if opened."""
    if _is_ci() and not force:
        return False
    try:
        import webbrowser
        webbrowser.open(f"file://{dash_path}")
        return True
    except Exception:
        return False


def _start_live_server(dash_path: str, port: int = 8976) -> None:
    """Spawn a detached live-server process that survives parent exit."""
    import socket
    import subprocess
    import webbrowser

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            webbrowser.open(f"http://127.0.0.1:{port}")
            return

    scripts_dir = str(Path(__file__).parent)
    subprocess.Popen(
        [sys.executable, "-c", f"""
import sys, os, threading
sys.path.insert(0, {scripts_dir!r})
os.chdir({scripts_dir!r})
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

dash = Path({dash_path!r})
repo_path = str(dash.parent.parent.parent)
_regen_lock = threading.Lock()

def _background_regenerate():
    if not _regen_lock.acquire(blocking=False):
        return
    try:
        from findings import generate_dashboard
        generate_dashboard(repo_path)
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        _regen_lock.release()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            content = dash.read_text(encoding="utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))
            threading.Thread(target=_background_regenerate, daemon=True).start()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"Error: {{e}}".encode())
    def log_message(self, fmt, *a):
        pass

server = HTTPServer(("127.0.0.1", {port}), Handler)
server.serve_forever()
"""],
        stdout=subprocess.DEVNULL,
        stderr=open(Path(dash_path).parent / "live_server.log", "w"),
        start_new_session=True,
    )

    import time
    for _ in range(10):
        time.sleep(0.2)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break

    url = f"http://127.0.0.1:{port}"
    print(f"  🔄 ライブサーバー起動: {url} (リロードで再生成)", file=sys.stderr, flush=True)
    webbrowser.open(url)


# ---------------------------------------------------------------------------
# Batch progress helpers
# ---------------------------------------------------------------------------

def _count_findings_on_disk(repo_path: str) -> int:
    """Count total findings currently stored in .delta-lint/findings/*.jsonl."""
    findings_dir = Path(repo_path) / ".delta-lint" / "findings"
    if not findings_dir.exists():
        return 0
    total = 0
    for jf in findings_dir.glob("*.jsonl"):
        try:
            total += sum(1 for line in jf.read_text().splitlines() if line.strip())
        except Exception:
            pass
    return total


def _format_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def _print_batch_progress(
    batch_idx: int,
    total_batches: int,
    batch_size: int,
    total_findings: int,
    elapsed: float,
    mode_label: str = "Wide",
):
    """Print a rich progress summary line after each batch completes."""
    done = batch_idx + 1
    pct = done * 100 // total_batches
    bar_len = 20
    filled = bar_len * done // total_batches
    bar = "█" * filled + "░" * (bar_len - filled)
    eta_str = ""
    if done < total_batches and elapsed > 0:
        avg_per_batch = elapsed / done
        remaining = avg_per_batch * (total_batches - done)
        eta_str = f"  ETA {_format_elapsed(remaining)}"
    print(
        f"\r  [{bar}] {done}/{total_batches} batches ({pct}%) │ "
        f"累積 {total_findings} findings │ {_format_elapsed(elapsed)}{eta_str}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Environment pre-check — auto-install & guided setup
# ---------------------------------------------------------------------------

def _check_environment(backend: str = "cli", verbose: bool = False) -> dict:
    """Check all external dependencies and attempt auto-install if missing."""
    import shutil
    import subprocess as _sp

    warnings: list[str] = []
    resolved_backend = backend
    degraded = False

    if not shutil.which("git"):
        warnings.append(
            "git not found. Diff-based scanning disabled. "
            "Use --files to specify files manually. "
            "Install: https://git-scm.com/downloads  "
            "macOS: xcode-select --install  "
            "Ubuntu: sudo apt install git"
        )
        degraded = True

    claude_available = bool(shutil.which("claude"))
    if not claude_available:
        if shutil.which("npm"):
            print("claude CLI not found. Attempting install...", file=sys.stderr)
            try:
                r = _sp.run(
                    ["npm", "install", "-g", "@anthropic-ai/claude-code"],
                    capture_output=True, text=True, timeout=120,
                )
                if r.returncode == 0 and shutil.which("claude"):
                    claude_available = True
                    print("  ✓ claude CLI installed.", file=sys.stderr)
            except (_sp.TimeoutExpired, OSError):
                pass

        if not claude_available:
            warnings.append(
                "claude CLI not available. "
                "Install: npm install -g @anthropic-ai/claude-code"
            )
            resolved_backend = "api"

    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not claude_available and not api_key:
        warnings.append(
            "No LLM backend available (no claude CLI, no API key). "
            "Set ANTHROPIC_API_KEY or install claude CLI to enable scanning. "
            "Continuing in dry-run mode."
        )
        degraded = True

    if resolved_backend == "api":
        try:
            import anthropic as _  # noqa: F401
        except ImportError:
            try:
                print("anthropic SDK not found. Attempting install...", file=sys.stderr)
                r = _sp.run(
                    [sys.executable, "-m", "pip", "install", "anthropic"],
                    capture_output=True, text=True, timeout=120,
                )
                if r.returncode == 0:
                    print("  ✓ anthropic SDK installed.", file=sys.stderr)
                else:
                    warnings.append(
                        "anthropic SDK not installed. Using raw HTTP fallback."
                    )
            except (_sp.TimeoutExpired, OSError):
                warnings.append("anthropic SDK install failed. Using raw HTTP fallback.")

    try:
        import yaml as _  # noqa: F401
    except ImportError:
        try:
            r = _sp.run(
                [sys.executable, "-m", "pip", "install", "pyyaml"],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0 and verbose:
                print("  ✓ PyYAML installed.", file=sys.stderr)
        except (_sp.TimeoutExpired, OSError):
            pass

    gh_available = bool(shutil.which("gh"))
    if not gh_available:
        if shutil.which("brew"):
            print("gh CLI not found. Attempting install via brew...", file=sys.stderr)
            try:
                r = _sp.run(
                    ["brew", "install", "gh"],
                    capture_output=True, text=True, timeout=600,
                )
                if r.returncode == 0 and shutil.which("gh"):
                    gh_available = True
                    print("  ✓ gh CLI installed.", file=sys.stderr)
            except (_sp.TimeoutExpired, OSError):
                pass
        if not gh_available and shutil.which("conda"):
            print("gh CLI not found. Attempting install via conda...", file=sys.stderr)
            try:
                r = _sp.run(
                    ["conda", "install", "-y", "-c", "conda-forge", "gh"],
                    capture_output=True, text=True, timeout=600,
                )
                if r.returncode == 0 and shutil.which("gh"):
                    gh_available = True
                    print("  ✓ gh CLI installed.", file=sys.stderr)
            except (_sp.TimeoutExpired, OSError):
                pass
        if not gh_available:
            warnings.append(
                "gh CLI not available. Issue/PR creation disabled. "
                "Install: brew install gh (macOS) / "
                "sudo apt install gh (Ubuntu) / "
                "conda install -c conda-forge gh"
            )

    if gh_available:
        try:
            r = _sp.run(
                ["gh", "auth", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                print("gh CLI not authenticated. Attempting browser-based login...",
                      file=sys.stderr)
                r = _sp.run(
                    ["gh", "auth", "login", "--web", "--git-protocol", "https"],
                    timeout=120,
                )
                if r.returncode == 0:
                    print("  ✓ gh CLI authenticated.", file=sys.stderr)
                else:
                    warnings.append(
                        "gh CLI installed but authentication failed. "
                        "Run 'gh auth login' manually. "
                        "Issue/PR creation will be skipped."
                    )
        except (_sp.TimeoutExpired, OSError):
            pass

    try:
        r = _sp.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.getcwd(),
        )
        if r.returncode == 0:
            default_branch = r.stdout.strip().replace("refs/remotes/origin/", "")
            if verbose:
                print(f"  Default branch: {default_branch}", file=sys.stderr)
    except (_sp.TimeoutExpired, OSError):
        pass

    try:
        r = _sp.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip() == "true":
            warnings.append(
                "Shallow clone detected — git history may be incomplete. "
                "Consider: git fetch --unshallow"
            )
    except (_sp.TimeoutExpired, OSError):
        pass

    for w in warnings:
        print(f"  ⚠ {w}", file=sys.stderr)

    return {"backend": resolved_backend, "warnings": warnings, "degraded": degraded,
            "gh_available": gh_available}


# ---------------------------------------------------------------------------
# Config file loading
# ---------------------------------------------------------------------------

def _auto_discover_docs(repo_path: str) -> list[str]:
    """Auto-discover document files for code × document contradiction checking."""
    repo = Path(repo_path).resolve()
    candidates = [
        "README.md", "ARCHITECTURE.md", "CONTRIBUTING.md",
        "DEVELOPMENT.md", "DESIGN.md", "API.md",
    ]
    found: list[str] = []
    for c in candidates:
        if (repo / c).exists():
            found.append(c)

    docs_dir = repo / "docs"
    if docs_dir.is_dir():
        for md in docs_dir.rglob("*.md"):
            rel = str(md.relative_to(repo))
            if rel not in found:
                found.append(rel)

    return found


def _load_json_safe(path: Path) -> dict:
    """Read a JSON file, returning empty dict on missing/invalid."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (override wins on conflict)."""
    merged = dict(base)
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def _load_config(repo_path: str = ".") -> dict:
    """Load config with 2-tier merge: ~/.delta-lint/config.json → .delta-lint/config.json."""
    global_config = _load_json_safe(Path.home() / ".delta-lint" / "config.json")
    local_config = _load_json_safe(
        Path(repo_path).resolve() / ".delta-lint" / "config.json"
    )
    if not global_config:
        return local_config
    if not local_config:
        return global_config
    return _deep_merge(global_config, local_config)


def _load_profile(profile_name: str, repo_path: str = ".") -> dict:
    """Load a scan profile from .delta-lint/profiles/<name>.yml or built-in profiles/."""
    try:
        import yaml
    except ImportError:
        print("⚠ PyYAML not installed. Profile loading requires PyYAML.", file=sys.stderr)
        return {}

    repo_profile = Path(repo_path).resolve() / ".delta-lint" / "profiles" / f"{profile_name}.yml"
    builtin_profile = Path(__file__).parent / "profiles" / f"{profile_name}.yml"

    profile_path = None
    if repo_profile.exists():
        profile_path = repo_profile
    elif builtin_profile.exists():
        profile_path = builtin_profile

    if not profile_path:
        print(f"⚠ Profile '{profile_name}' not found.", file=sys.stderr)
        print(f"  Searched: {repo_profile}", file=sys.stderr)
        print(f"           {builtin_profile}", file=sys.stderr)
        available = []
        for d in [repo_profile.parent, builtin_profile.parent]:
            if d.exists():
                available.extend(p.stem for p in d.glob("*.yml") if not p.stem.startswith("_"))
        if available:
            print(f"  Available: {', '.join(sorted(set(available)))}", file=sys.stderr)
        return {}

    try:
        data = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠ Failed to load profile '{profile_name}': {e}", file=sys.stderr)
        return {}

    if not isinstance(data, dict):
        return {}

    result = {}
    if "config" in data and isinstance(data["config"], dict):
        result.update(data["config"])
    if "policy" in data and isinstance(data["policy"], dict):
        result["_profile_policy"] = data["policy"]

    return result


def _apply_profile_policy(args, profile: dict, repo_path: str):
    """Apply profile's policy section to the scan args."""
    policy = profile.get("_profile_policy")
    if not policy:
        return

    if not hasattr(args, '_profile_policy'):
        args._profile_policy = {}
    args._profile_policy = policy


# ---------------------------------------------------------------------------
# File category matching
# ---------------------------------------------------------------------------

def _match_file_category(filepath: str, categories: dict) -> str | None:
    """Match a file path against category patterns. Returns category name or None."""
    import fnmatch

    for cat_name, cat_config in categories.items():
        patterns = cat_config.get("patterns", [])
        for pattern in patterns:
            if fnmatch.fnmatch(filepath, pattern):
                return cat_name
    return None


def _apply_category_severity_boost(findings: list[dict], categories: dict,
                                   verbose: bool = False) -> list[dict]:
    """Apply severity_boost from file categories to findings."""
    if not categories:
        return findings

    SEVERITY_LEVELS = ["low", "medium", "high"]
    boosted_count = 0

    for f in findings:
        if f.get("parse_error"):
            continue
        loc = f.get("location", {})
        if not isinstance(loc, dict):
            continue

        file_a = loc.get("file_a", "")
        file_b = loc.get("file_b", "")

        boosts = []
        for fp in (file_a, file_b):
            if fp:
                cat = _match_file_category(fp, categories)
                if cat:
                    boost = categories[cat].get("severity_boost", 0)
                    boosts.append(boost)

        if not boosts:
            continue

        boost = min(boosts, key=abs) if any(b == 0 for b in boosts) else max(boosts, key=abs)

        if boost == 0:
            continue

        current_sev = f.get("severity", "medium").lower()
        current_idx = SEVERITY_LEVELS.index(current_sev) if current_sev in SEVERITY_LEVELS else 1
        new_idx = max(0, min(len(SEVERITY_LEVELS) - 1, current_idx + boost))

        if new_idx != current_idx:
            new_sev = SEVERITY_LEVELS[new_idx]
            f["_original_severity"] = current_sev
            f["severity"] = new_sev
            boosted_count += 1

    if verbose and boosted_count:
        print(f"  Category severity boost: {boosted_count} finding(s) adjusted",
              file=sys.stderr)

    return findings


def _apply_config_to_parser(parser, config: dict):
    """Override parser defaults with config values. CLI flags still win."""
    mapping = {
        "lang": "lang",
        "backend": "backend",
        "severity": "severity",
        "model": "model",
        "default_model": "model",
        "verbose": "verbose",
        "semantic": "semantic",
        "autofix": "autofix",
        "diff_target": "diff_target",
        "output_format": "output_format",
        "format": "output_format",
        "no_learn": "no_learn",
        "no_cache": "no_cache",
        "no_verify": "no_verify",
        "no_open": "no_open",
    }
    new_defaults = {}
    for config_key, dest in mapping.items():
        if config_key in config:
            new_defaults[dest] = config[config_key]
    if new_defaults:
        parser.set_defaults(**new_defaults)


# ---------------------------------------------------------------------------
# Scan log utilities
# ---------------------------------------------------------------------------

def _find_latest_scan_log(repo_path: str) -> Path | None:
    """Find the most recent scan log in .delta-lint/."""
    log_dir = Path(repo_path) / ".delta-lint"
    if not log_dir.exists():
        return None
    logs = sorted(log_dir.glob("delta_lint_*.json"), reverse=True)
    return logs[0] if logs else None


def _load_scan_log(log_path: Path) -> dict | None:
    """Load and parse a scan log file."""
    try:
        return json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error reading scan log {log_path}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Baseline comparison
# ---------------------------------------------------------------------------

def _build_baseline_hashes(repo_path: str, baseline_ref: str,
                           verbose: bool = False) -> set[str] | None:
    """Run a scan at the baseline ref and collect finding hashes."""
    import subprocess as _sp

    try:
        result = _sp.run(
            ["git", "rev-parse", baseline_ref],
            capture_output=True, text=True, timeout=10,
            cwd=repo_path,
        )
        if result.returncode != 0:
            print(f"  ⚠ Cannot resolve baseline ref '{baseline_ref}': {result.stderr.strip()}",
                  file=sys.stderr)
            return None
        baseline_sha = result.stdout.strip()[:12]
    except (_sp.TimeoutExpired, OSError):
        return None

    snapshot_path = Path(repo_path) / ".delta-lint" / "baselines" / f"{baseline_sha}.json"
    if snapshot_path.exists():
        try:
            data = json.loads(snapshot_path.read_text(encoding="utf-8"))
            hashes = set(data.get("finding_hashes", []))
            if verbose:
                print(f"  Baseline loaded from snapshot: {len(hashes)} finding(s)",
                      file=sys.stderr)
            return hashes
        except (OSError, json.JSONDecodeError):
            pass

    if verbose:
        print(f"  No baseline snapshot for {baseline_sha}. "
              f"Use current scan to create one with --baseline-save.", file=sys.stderr)
    return None


def _save_baseline_snapshot(repo_path: str, findings: list[dict],
                            verbose: bool = False) -> Path | None:
    """Save current findings as a baseline snapshot keyed by HEAD commit."""
    import subprocess as _sp

    try:
        result = _sp.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=repo_path,
        )
        if result.returncode != 0:
            return None
        head_sha = result.stdout.strip()[:12]
    except (_sp.TimeoutExpired, OSError):
        return None

    baselines_dir = Path(repo_path) / ".delta-lint" / "baselines"
    baselines_dir.mkdir(parents=True, exist_ok=True)

    finding_hashes = []
    for f in findings:
        if f.get("parse_error"):
            continue
        fh = _compute_finding_identity(f)
        if fh:
            finding_hashes.append(fh)

    snapshot = {
        "commit": head_sha,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
        "findings_count": len(finding_hashes),
        "finding_hashes": finding_hashes,
    }

    snapshot_path = baselines_dir / f"{head_sha}.json"
    snapshot_path.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if verbose:
        print(f"  Baseline snapshot saved: {snapshot_path} ({len(finding_hashes)} finding(s))",
              file=sys.stderr)
    return snapshot_path


def _compute_finding_identity(f: dict) -> str | None:
    """Compute a stable identity for a finding (pattern + sorted file pair)."""
    import hashlib

    loc = f.get("location", {})
    if not isinstance(loc, dict):
        return None
    file_a = loc.get("file_a", "")
    file_b = loc.get("file_b", "")
    pattern = f.get("pattern", "")
    if not file_a and not file_b:
        return None
    files = sorted([file_a, file_b])
    key = f"{files[0]}:{files[1]}:{pattern}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _filter_new_findings(findings: list[dict],
                         baseline_hashes: set[str]) -> tuple[list[dict], int]:
    """Return only findings NOT in the baseline."""
    new_findings = []
    baseline_count = 0
    for f in findings:
        fh = _compute_finding_identity(f)
        if fh and fh in baseline_hashes:
            baseline_count += 1
        else:
            new_findings.append(f)
    return new_findings, baseline_count


# ---------------------------------------------------------------------------
# Scan axes normalization
# ---------------------------------------------------------------------------

def _normalize_scan_axes(args):
    """Normalize legacy flags (--smart, --deep, --full) to 3-axis model."""
    if args.scope is not None:
        args._scope = args.scope
    elif getattr(args, 'smart', False):
        args._scope = "smart"
    elif getattr(args, 'files', None):
        args._scope = "files"
    else:
        args._scope = "diff"

    _depth_aliases = {"graph": "deep", "1hop": "default"}
    if args.depth is not None:
        args._depth = _depth_aliases.get(args.depth, args.depth)
    elif getattr(args, 'deep', False):
        args._depth = "deep"
    elif args._scope == "pr":
        args._depth = "deep"
    else:
        args._depth = "default"

    if args.lens is not None:
        args._lens = args.lens
    elif getattr(args, 'full', False):
        args._lens = "stress"
    else:
        args._lens = "default"

    if getattr(args, 'verbose', False):
        print(f"  Scan axes: scope={args._scope} depth={args._depth} lens={args._lens}",
              file=sys.stderr)
