"""
Stress-test engine for delta-lint.

Generates virtual modifications and runs scan on each to build a
per-file "landmine map" showing which areas break most easily.

Pipeline:
  Step 0:   Structural analysis (claude -p, $0)
  Step 0.5: Existing bug scan — scan hotspot clusters for current contradictions
  Step 1:   Virtual modification generation (claude -p, $0)
  Step 2:   Scan each modification (existing detect engine, claude -p, $0)

All LLM calls use claude -p (subscription CLI) for $0 cost.
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from retrieval import (
    ModuleContext,
    FileContext,
    build_context,
    filter_source_files,
    _read_file_safe,
)
from detector import detect


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROMPT_DIR = Path(__file__).parent / "prompts"
HEAD_LINES = 50  # Lines to read from each file for structural analysis
MAX_FILES_FOR_STRUCTURE = 80  # Cap files sent to structure analysis


def _sample_across_dirs(files: list[str], max_count: int) -> list[str]:
    """Sample files evenly across top-level directories.

    Avoids alphabetical bias where e.g. 'apps/design-system' consumes
    all slots before 'apps/studio' is reached.
    """
    from collections import defaultdict

    # Group by top 2 directory levels (e.g. "apps/studio")
    groups: dict[str, list[str]] = defaultdict(list)
    for f in files:
        parts = f.split("/")
        key = "/".join(parts[:min(2, len(parts))])
        groups[key].append(f)

    # Round-robin across groups
    sampled: list[str] = []
    group_iters = {k: iter(v) for k, v in sorted(groups.items())}

    while len(sampled) < max_count and group_iters:
        exhausted = []
        for key, it in group_iters.items():
            if len(sampled) >= max_count:
                break
            val = next(it, None)
            if val is None:
                exhausted.append(key)
            else:
                sampled.append(val)
        for key in exhausted:
            del group_iters[key]

    return sampled


# ---------------------------------------------------------------------------
# Step 0: Structural analysis
# ---------------------------------------------------------------------------

def analyze_structure(repo_path: str, verbose: bool = False) -> dict:
    """Analyze codebase structure via claude -p.

    Reads file headers and asks LLM to identify roles, dependencies,
    and implicit constraints.
    """
    if verbose:
        print("[step 0] Analyzing codebase structure...", file=sys.stderr)

    repo = Path(repo_path)

    # Get source files via git ls-files
    result = subprocess.run(
        ["git", "ls-files"],
        capture_output=True, text=True, cwd=repo_path, timeout=10,
    )
    all_files = filter_source_files(result.stdout.strip().split("\n"))

    if verbose:
        print(f"  Found {len(all_files)} source files", file=sys.stderr)

    # Sample files evenly across directories (avoid alphabetical bias)
    files_to_analyze = _sample_across_dirs(all_files, MAX_FILES_FOR_STRUCTURE)

    # Read first N lines of each file
    file_previews = []
    for fpath in files_to_analyze:
        full = repo / fpath
        if not full.exists():
            continue
        content = _read_file_safe(full)
        if content is None:
            continue
        lines = content.split("\n")[:HEAD_LINES]
        preview = "\n".join(lines)
        file_previews.append(f"=== {fpath} ===\n{preview}")

    # Load prompt template
    prompt_template = (PROMPT_DIR / "structure_analysis.md").read_text(encoding="utf-8")
    prompt = prompt_template + "\n\n" + "\n\n".join(file_previews)

    # Truncate if too large
    if len(prompt) > 80_000:
        prompt = prompt[:80_000] + "\n... (truncated)"

    if verbose:
        print(f"  Sending {len(file_previews)} file previews to claude -p ({len(prompt)} chars)", file=sys.stderr)

    raw = _call_claude(prompt)
    structure = _parse_json_response(raw)

    if verbose:
        modules = structure.get("modules", [])
        hotspots = structure.get("hotspots", [])
        print(f"  Identified {len(modules)} modules, {len(hotspots)} hotspots", file=sys.stderr)

    return structure


# ---------------------------------------------------------------------------
# Step 0.5: Existing bug scan — scan hotspot clusters directly
# ---------------------------------------------------------------------------

_EXISTING_LANG_INSTRUCTIONS = {
    "en": "",
    "ja": (
        "## Language\n\n"
        "Write the `contradiction`, `user_impact`, `reproduction`, and `internal_evidence` fields in **Japanese**. "
        "Keep `pattern`, `severity`, `bug_class`, and `location` fields in English/emoji. "
        "Example: `\"user_impact\": \"デフォルト設定でLoRAファインチューニングを実行するとAttributeErrorでクラッシュする\"`"
    ),
}


def _load_existing_prompt(lang: str = "en") -> str:
    """Load the existing-bug-specific detection prompt."""
    prompt = (PROMPT_DIR / "detect_existing.md").read_text(encoding="utf-8")
    lang_instruction = _EXISTING_LANG_INSTRUCTIONS.get(lang, "")
    return prompt.replace("{lang_instruction}", lang_instruction)


def _scan_cluster(
    cluster: dict,
    index: int,
    total: int,
    repo_path: str,
    backend: str,
    verbose: bool,
    lang: str = "en",
) -> dict:
    """Scan a file cluster for existing contradictions. Thread-safe.

    Uses detect_existing.md prompt which classifies findings as:
    🔴 実バグ / 🟡 サイレント障害 / ⚪ 潜在リスク
    and requires concrete user_impact and reproduction fields.
    """
    center = cluster["center"]
    files = cluster["files"]

    if verbose:
        print(f"[step 0.5] [{index}/{total}] Scanning cluster: {center}", file=sys.stderr)

    last_error = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            context = build_context(repo_path, files)

            if not context.target_files:
                if verbose:
                    print(f"  [{index}/{total}] Skipped (no readable files)", file=sys.stderr)
                return {"cluster": cluster, "findings": []}

            # Use existing-bug-specific prompt (not the stress-test one)
            system_prompt = _load_existing_prompt(lang=lang)
            from detector import build_user_prompt, _parse_response, _detect_cli, _cli_available
            user_prompt = build_user_prompt(context, repo_name=Path(repo_path).name)

            if backend == "cli" and _cli_available():
                raw = _detect_cli(system_prompt, user_prompt)
            else:
                # Fallback to standard detect with default prompt
                findings = detect(
                    context,
                    repo_name=Path(repo_path).name,
                    backend=backend,
                )
                findings = [f for f in findings if not f.get("parse_error")]
                if verbose:
                    print(f"  [{index}/{total}] Found {len(findings)} finding(s) (fallback prompt)", file=sys.stderr)
                return {"cluster": cluster, "findings": findings}

            findings = _parse_response(raw)
            findings = [f for f in findings if not f.get("parse_error")]

            if verbose:
                print(f"  [{index}/{total}] Found {len(findings)} finding(s)", file=sys.stderr)

            return {"cluster": cluster, "findings": findings}

        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                if verbose:
                    print(f"  [{index}/{total}] Retry ({e})", file=sys.stderr)
            else:
                if verbose:
                    print(f"  [{index}/{total}] Failed: {e}", file=sys.stderr)

    return {"cluster": cluster, "findings": [], "error": str(last_error)}


def scan_existing(
    structure: dict,
    repo_path: str,
    backend: str = "cli",
    verbose: bool = False,
    parallel: int = 1,
    lang: str = "en",
) -> list[dict]:
    """Scan hotspot file clusters for existing contradictions.

    Uses structure.json hotspots + module dependencies to build clusters,
    then runs detect() on each cluster WITHOUT virtual modifications.
    This finds bugs that exist RIGHT NOW in the codebase.
    """
    if verbose:
        print("[step 0.5] Scanning for existing contradictions...", file=sys.stderr)

    hotspots = structure.get("hotspots", [])
    modules = structure.get("modules", [])

    if not hotspots:
        if verbose:
            print("  No hotspots found, skipping existing scan", file=sys.stderr)
        return []

    # Build dependency lookup from structure.json modules
    dep_map: dict[str, list[str]] = {}
    for mod in modules:
        path = mod.get("path", "")
        deps = mod.get("dependencies", [])
        if path:
            dep_map[path] = deps

    # Build clusters: each hotspot + its dependencies
    clusters: list[dict] = []
    seen_centers: set[str] = set()

    for hs in hotspots:
        center = hs.get("path", hs.get("file", ""))
        if not center or center in seen_centers:
            continue
        seen_centers.add(center)

        files = [center]
        # Add dependencies from structure.json
        for dep in dep_map.get(center, []):
            if dep not in files:
                files.append(dep)

        # Also add modules that depend ON this hotspot (reverse deps)
        for mod in modules:
            if center in mod.get("dependencies", []):
                mod_path = mod.get("path", "")
                if mod_path and mod_path not in files:
                    files.append(mod_path)

        clusters.append({
            "center": center,
            "reason": hs.get("reason", ""),
            "files": files,
        })

    if verbose:
        print(f"  {len(clusters)} hotspot clusters to scan", file=sys.stderr)

    total = len(clusters)

    if parallel <= 1:
        return [
            _scan_cluster(c, i, total, repo_path, backend, verbose, lang=lang)
            for i, c in enumerate(clusters, 1)
        ]

    # Parallel execution
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if verbose:
        print(f"[step 0.5] Running {total} cluster scans with {parallel} workers", file=sys.stderr)

    results = [None] * total
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {
            pool.submit(_scan_cluster, c, i, total, repo_path, backend, verbose, lang): i - 1
            for i, c in enumerate(clusters, 1)
        }
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()

    return results


# ---------------------------------------------------------------------------
# Step 1: Generate virtual modifications
# ---------------------------------------------------------------------------

def generate_modifications(
    structure: dict,
    repo_path: str,
    n: int = 25,
    verbose: bool = False,
) -> list[dict]:
    """Generate virtual modifications via claude -p.

    Uses structural analysis + git history to create realistic
    virtual code changes for stress testing.
    """
    if verbose:
        print(f"[step 1] Generating {n} virtual modifications...", file=sys.stderr)

    # Get recent git log
    result = subprocess.run(
        ["git", "log", "--oneline", "-50"],
        capture_output=True, text=True, cwd=repo_path, timeout=10,
    )
    git_log = result.stdout.strip()

    # Load prompt template
    prompt_template = (PROMPT_DIR / "generate_modifications.md").read_text(encoding="utf-8")
    prompt = prompt_template.replace("{n}", str(n))
    prompt = prompt.replace("{structure}", json.dumps(structure, indent=2, ensure_ascii=False))
    prompt = prompt.replace("{git_log}", git_log)

    if len(prompt) > 80_000:
        prompt = prompt[:80_000] + "\n... (truncated)"

    if verbose:
        print(f"  Prompt size: {len(prompt)} chars", file=sys.stderr)

    raw = _call_claude(prompt)
    modifications = _parse_json_response(raw)

    if isinstance(modifications, dict):
        modifications = modifications.get("modifications", [modifications])
    if not isinstance(modifications, list):
        modifications = []

    # Assign IDs if missing
    for i, mod in enumerate(modifications, 1):
        if "id" not in mod:
            mod["id"] = i

    if verbose:
        print(f"  Generated {len(modifications)} modifications", file=sys.stderr)
        for mod in modifications[:5]:
            cat = mod.get("category", "?")
            desc = mod.get("description", "?")[:60]
            print(f"    [{cat}] {mod.get('file', '?')}: {desc}", file=sys.stderr)
        if len(modifications) > 5:
            print(f"    ... and {len(modifications) - 5} more", file=sys.stderr)

    return modifications


# ---------------------------------------------------------------------------
# Step 2: Run scan on each modification
# ---------------------------------------------------------------------------

MAX_RETRIES = 1  # Retry failed scans once


def _scan_one(
    mod: dict,
    index: int,
    total: int,
    repo_path: str,
    backend: str,
    verbose: bool,
) -> dict:
    """Scan a single virtual modification. Thread-safe. Retries on failure."""
    target_file = mod.get("file", "")
    affected = mod.get("affected_files", [])
    description = mod.get("description", "")

    if verbose:
        print(f"[step 2] [{index}/{total}] Scanning: {target_file}", file=sys.stderr)

    scan_files = []
    if target_file:
        scan_files.append(target_file)
    for af in affected:
        if af not in scan_files:
            scan_files.append(af)

    if not scan_files:
        if verbose:
            print(f"  [{index}/{total}] Skipped (no files)", file=sys.stderr)
        return {"modification": mod, "findings": []}

    last_error = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            context = build_context(repo_path, scan_files)

            mod_context = (
                f"VIRTUAL MODIFICATION (stress-test):\n"
                f"File: {target_file}\n"
                f"Function: {mod.get('function', 'N/A')}\n"
                f"Change: {description}\n"
                f"Category: {mod.get('category', 'N/A')}\n\n"
                f"Analyze the code below assuming this modification has been made. "
                f"Look for structural contradictions that would arise FROM this change."
            )
            context.target_files.insert(0, FileContext(
                path="[virtual-modification]",
                content=mod_context,
                is_target=True,
            ))

            findings = detect(
                context,
                repo_name=Path(repo_path).name,
                backend=backend,
            )
            findings = [f for f in findings if not f.get("parse_error")]

            if verbose:
                print(f"  [{index}/{total}] Found {len(findings)} contradiction(s)", file=sys.stderr)

            return {"modification": mod, "findings": findings}

        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                if verbose:
                    print(f"  [{index}/{total}] Retry ({e})", file=sys.stderr)
            else:
                if verbose:
                    print(f"  [{index}/{total}] Failed: {e}", file=sys.stderr)

    return {"modification": mod, "findings": [], "error": str(last_error)}


def run_scans(
    modifications: list[dict],
    repo_path: str,
    backend: str = "cli",
    verbose: bool = False,
    parallel: int = 1,
) -> list[dict]:
    """Run scan on each virtual modification using existing detect engine.

    Args:
        parallel: Number of concurrent scans (default: 1 = sequential)
    """
    total = len(modifications)

    if parallel <= 1:
        return [
            _scan_one(mod, i, total, repo_path, backend, verbose)
            for i, mod in enumerate(modifications, 1)
        ]

    # Parallel execution via ThreadPoolExecutor
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if verbose:
        print(f"[step 2] Running {total} scans with {parallel} workers", file=sys.stderr)

    results = [None] * total
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {
            pool.submit(_scan_one, mod, i, total, repo_path, backend, verbose): i - 1
            for i, mod in enumerate(modifications, 1)
        }
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call_claude(prompt: str) -> str:
    """Call claude -p (subscription CLI, $0 cost)."""
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p failed: {result.stderr[:300]}")
    return result.stdout


def _parse_json_response(raw: str) -> dict | list:
    """Parse JSON from LLM response, handling markdown code blocks."""
    text = raw.strip()

    # Extract from markdown code block
    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object or array
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass

    return {}


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

BATCH_SIZE = 10  # Save results every N scans
CONVERGENCE_WINDOW = 20  # Check convergence over last N scans
MIN_SCANS_BEFORE_CONVERGENCE = 30  # Don't stop before this many


def estimate_n(n_source_files: int) -> int:
    """Auto-determine modification count based on repo size.

    Heuristic: aim for ~20% coverage of source files, clamped to [20, 300].
    """
    n = max(20, min(int(n_source_files * 0.2), 300))
    # Round to nearest 10
    return ((n + 5) // 10) * 10


def _check_convergence(results: list[dict], verbose: bool) -> bool:
    """Check if the risk map has converged (no new files discovered recently).

    Returns True if we should stop scanning.
    """
    if len(results) < MIN_SCANS_BEFORE_CONVERGENCE:
        return False

    # Files discovered in the earlier portion
    early = results[:-CONVERGENCE_WINDOW]
    recent = results[-CONVERGENCE_WINDOW:]

    early_files: set[str] = set()
    for r in early:
        mod = r.get("modification", {})
        if r.get("findings"):
            f = mod.get("file", "")
            if f:
                early_files.add(f)
            for af in mod.get("affected_files", []):
                early_files.add(af)

    new_files = 0
    for r in recent:
        mod = r.get("modification", {})
        if r.get("findings"):
            f = mod.get("file", "")
            if f and f not in early_files:
                new_files += 1
            for af in mod.get("affected_files", []):
                if af not in early_files:
                    new_files += 1

    if verbose:
        print(f"  [convergence] {new_files} new files in last {CONVERGENCE_WINDOW} scans", file=sys.stderr)

    return new_files == 0


def _get_hotspot_summary(results: list[dict], n_top: int = 10) -> str:
    """Build a hotspot summary string from current results for focused generation."""
    from collections import Counter
    file_hits = Counter()
    for r in results:
        if not r.get("findings"):
            continue
        mod = r.get("modification", {})
        f = mod.get("file", "")
        if f:
            file_hits[f] += len(r["findings"])
        for af in mod.get("affected_files", []):
            file_hits[af] += 1

    lines = []
    for f, count in file_hits.most_common(n_top):
        lines.append(f"- {f}: {count} findings")
    return "\n".join(lines) if lines else "No hotspots identified yet."


def _get_tested_summary(results: list[dict]) -> str:
    """Build summary of already-tested modifications to avoid repetition."""
    lines = []
    for r in results:
        mod = r.get("modification", {})
        f = mod.get("file", "")
        desc = mod.get("description", "")[:80]
        lines.append(f"- {f}: {desc}")
    return "\n".join(lines[-30:])  # Last 30 to keep prompt size reasonable


def generate_focused_modifications(
    structure: dict,
    results: list[dict],
    repo_path: str,
    n: int = 10,
    verbose: bool = False,
) -> list[dict]:
    """Generate focused modifications targeting discovered hotspots."""
    if verbose:
        print(f"[adaptive] Generating {n} focused modifications on hotspots...", file=sys.stderr)

    hotspots = _get_hotspot_summary(results)
    already_tested = _get_tested_summary(results)

    prompt_template = (PROMPT_DIR / "generate_focused_modifications.md").read_text(encoding="utf-8")
    prompt = prompt_template.replace("{n}", str(n))
    prompt = prompt.replace("{structure}", json.dumps(structure, indent=2, ensure_ascii=False))
    prompt = prompt.replace("{hotspots}", hotspots)
    prompt = prompt.replace("{already_tested}", already_tested)

    if len(prompt) > 80_000:
        prompt = prompt[:80_000] + "\n... (truncated)"

    raw = _call_claude(prompt)
    modifications = _parse_json_response(raw)

    if isinstance(modifications, dict):
        modifications = modifications.get("modifications", [modifications])
    if not isinstance(modifications, list):
        modifications = []

    # Assign IDs continuing from current count
    base_id = len(results) + 1
    for i, mod in enumerate(modifications):
        mod["id"] = base_id + i
        mod.setdefault("category", "focused")

    if verbose:
        print(f"  Generated {len(modifications)} focused modifications", file=sys.stderr)

    return modifications


def _save_results(out: Path, results: list[dict], metadata: dict, verbose: bool):
    """Save current results to results.json (incremental update)."""
    results_path = out / "results.json"
    output_data = {
        "metadata": {**metadata, "n_completed": len(results)},
        "results": results,
    }
    results_path.write_text(
        json.dumps(output_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if verbose:
        total_findings = sum(len(r.get("findings", [])) for r in results)
        hit_mods = sum(1 for r in results if r.get("findings"))
        print(f"  [checkpoint] {len(results)} scans, {hit_mods} hits, {total_findings} findings", file=sys.stderr)


def _update_heatmap(out: Path, verbose: bool):
    """Regenerate heatmap from current results.json."""
    try:
        from visualize import generate_heatmap
        generate_heatmap(
            results_path=str(out / "results.json"),
            output_path=str(out / "landmine_map.html"),
        )
    except Exception as e:
        if verbose:
            print(f"  [heatmap] update failed: {e}", file=sys.stderr)


def run_stress_test(
    repo_path: str,
    n_modifications: int = 0,
    backend: str = "cli",
    verbose: bool = False,
    output_dir: str | None = None,
    parallel: int = 1,
    visualize: bool = False,
    lang: str = "en",
) -> list[dict]:
    """Main entry point — autonomous adaptive stress-test.

    Autonomy features:
    - n=0 (default): auto-determines count from repo size
    - Incremental saves every BATCH_SIZE (10) scans
    - After initial batch, generates focused modifications targeting hotspots
    - Auto-converges when no new files are discovered
    - Retries failed scans automatically
    """
    repo_path = str(Path(repo_path).resolve())
    if output_dir is None:
        output_dir = str(Path(repo_path) / ".delta-lint" / "stress-test")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Step 0: Structure analysis
    structure = analyze_structure(repo_path, verbose=verbose)
    structure_path = out / "structure.json"
    structure_path.write_text(
        json.dumps(structure, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if verbose:
        print(f"  Saved: {structure_path}", file=sys.stderr)

    # Step 0.5: Scan existing contradictions in hotspot clusters
    existing_results = scan_existing(
        structure, repo_path,
        backend=backend, verbose=verbose, parallel=parallel, lang=lang,
    )
    existing_findings_path = out / "existing_findings.json"
    existing_data = {
        "metadata": {
            "repo": repo_path,
            "repo_name": Path(repo_path).name,
            "timestamp": timestamp,
            "n_clusters": len(existing_results),
        },
        "results": existing_results,
    }
    existing_findings_path.write_text(
        json.dumps(existing_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if verbose:
        n_findings = sum(len(r.get("findings", [])) for r in existing_results)
        n_hits = sum(1 for r in existing_results if r.get("findings"))
        print(f"  Saved: {existing_findings_path}", file=sys.stderr)
        print(f"  {n_hits}/{len(existing_results)} clusters had existing contradictions ({n_findings} total)", file=sys.stderr)

    # Auto-determine n from repo size if not specified
    if n_modifications <= 0:
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True, text=True, cwd=repo_path, timeout=10,
        )
        n_source = len(filter_source_files(result.stdout.strip().split("\n")))
        n_modifications = estimate_n(n_source)
        if verbose:
            print(f"[auto] {n_source} source files → n={n_modifications} modifications", file=sys.stderr)

    metadata = {
        "repo": repo_path,
        "repo_name": Path(repo_path).name,
        "n_modifications": n_modifications,
        "timestamp": timestamp,
        "backend": backend,
    }

    # Step 1: Generate initial modifications
    initial_n = min(n_modifications, BATCH_SIZE * 3)  # First 30 from broad generation
    modifications = generate_modifications(
        structure, repo_path, n=initial_n, verbose=verbose,
    )
    mods_path = out / "modifications.json"
    mods_path.write_text(
        json.dumps(modifications, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if verbose:
        print(f"  Saved: {mods_path}", file=sys.stderr)

    # Step 2: Adaptive scan loop
    all_results: list[dict] = []
    pending = list(modifications)
    converged = False

    while len(all_results) < n_modifications and not converged:
        # Take next batch from pending
        batch = pending[:BATCH_SIZE]
        pending = pending[BATCH_SIZE:]

        if not batch:
            # No more pending — generate focused modifications on hotspots
            remaining = n_modifications - len(all_results)
            focus_n = min(BATCH_SIZE, remaining)
            if focus_n <= 0:
                break
            batch = generate_focused_modifications(
                structure, all_results, repo_path, n=focus_n, verbose=verbose,
            )
            if not batch:
                if verbose:
                    print("[adaptive] No more modifications to generate. Stopping.", file=sys.stderr)
                break

        batch_results = run_scans(
            batch, repo_path, backend=backend, verbose=verbose, parallel=parallel,
        )
        all_results.extend(batch_results)

        # Save checkpoint
        metadata["n_completed"] = len(all_results)
        _save_results(out, all_results, metadata, verbose)

        if visualize:
            _update_heatmap(out, verbose)

        # Check convergence
        if _check_convergence(all_results, verbose):
            converged = True
            if verbose:
                print(f"[adaptive] Converged at {len(all_results)} scans. Map is stable.", file=sys.stderr)

    # Final summary
    total_findings = sum(len(r.get("findings", [])) for r in all_results)
    hit_mods = sum(1 for r in all_results if r.get("findings"))
    if verbose:
        status = "converged" if converged else "completed"
        print(f"\n[summary] {status} after {len(all_results)} scans", file=sys.stderr)
        print(f"[summary] {hit_mods}/{len(all_results)} modifications triggered contradictions", file=sys.stderr)
        print(f"[summary] {total_findings} total findings", file=sys.stderr)
        print(f"  Saved: {out / 'results.json'}", file=sys.stderr)

    return all_results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="delta-lint stress-test: generate virtual modifications and scan for contradictions"
    )
    parser.add_argument("--repo", required=True, help="Repository path")
    parser.add_argument("--n", type=int, default=0, help="Number of modifications (0=auto-determine from repo size)")
    parser.add_argument("--backend", default="cli", choices=["cli", "api"], help="LLM backend (default: cli = $0)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print progress to stderr")
    parser.add_argument("--output-dir", default=None, help="Output directory")
    parser.add_argument("--parallel", type=int, default=1, help="Concurrent scans (default: 1, recommended max: 10)")
    parser.add_argument("--visualize", action="store_true", help="Generate HTML heatmap after scan")
    parser.add_argument("--lang", default="en", choices=["en", "ja"], help="Output language for findings (default: en)")
    parser.add_argument("--structure-only", action="store_true", help="Run only structure analysis (Step 0), then exit")

    args = parser.parse_args()

    if args.structure_only:
        structure = analyze_structure(args.repo, verbose=args.verbose)
        output_dir = Path(args.output_dir or Path(args.repo) / ".delta-lint" / "stress-test")
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(output_dir / "structure.json", "w") as f:
            json.dump(structure, f, indent=2, ensure_ascii=False)
        # Print summary for immediate display
        modules = structure.get("modules", [])
        hotspots = structure.get("hotspots", [])
        constraints = structure.get("implicit_constraints", [])
        print(f"modules: {len(modules)}")
        print(f"hotspots: {len(hotspots)}")
        for h in hotspots[:5]:
            print(f"  hotspot: {h.get('path', h.get('file', ''))} — {h.get('reason', '')}")
        for c in constraints[:5]:
            print(f"  constraint: {c}")
        sys.exit(0)

    results = run_stress_test(
        repo_path=args.repo,
        n_modifications=args.n,
        backend=args.backend,
        verbose=args.verbose,
        output_dir=args.output_dir,
        parallel=args.parallel,
        visualize=args.visualize,
        lang=args.lang,
    )

    # Summary output
    total_findings = sum(len(r.get("findings", [])) for r in results)
    hit_mods = sum(1 for r in results if r.get("findings"))
    print(f"{hit_mods}/{len(results)} modifications triggered {total_findings} findings")
