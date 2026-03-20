"""cmd_scan – scan commands extracted from cli.py for modularity."""

import os
import sys
from datetime import date, datetime
from pathlib import Path

from retrieval import get_changed_files, filter_source_files, build_context
from detector import detect
from output import filter_findings, print_results, save_log
from suppress import (
    SuppressEntry,
    compute_finding_hash,
    compute_code_hash,
    load_suppressions,
    save_suppressions,
)
from cli_utils import (
    _check_environment,
    _adaptive_since,
    _count_findings_on_disk,
    _format_elapsed,
    _print_batch_progress,
    _open_dashboard,
    _auto_discover_docs,
    _load_config,
    _apply_category_severity_boost,
    _build_baseline_hashes,
    _save_baseline_snapshot,
    _filter_new_findings,
)


def cmd_scan_deep(args):
    """Run deep structural scan using regex + contract graph + LLM verification."""
    repo_path = str(Path(args.repo).resolve())
    verbose = getattr(args, "verbose", False)
    workers = getattr(args, "deep_workers", 4)

    print("── δ-lint deep scan ──", file=sys.stderr)

    # Phase 0: Surface extraction
    from surface_extractor import extract_surfaces, collect_all_source_files
    all_files = collect_all_source_files(repo_path)
    if not all_files:
        print("No source files found.", file=sys.stderr)
        sys.exit(0)
    print(f"  Phase 0: Extracting surfaces from {len(all_files)} files...", file=sys.stderr)
    surfaces = extract_surfaces(repo_path, all_files, verbose=verbose)

    # Phase 1: Contract graph
    from contract_graph import build_index, detect_mismatches
    index = build_index(surfaces)
    candidates = detect_mismatches(index, verbose=verbose)

    if not candidates:
        print("  No structural mismatches detected.", file=sys.stderr)
        sys.exit(0)

    # Phase 2: LLM verification
    from deep_verifier import verify_all
    findings = verify_all(candidates, repo_path, max_workers=workers, verbose=verbose)

    if not findings:
        print("  All candidates were rejected by verification.", file=sys.stderr)
        sys.exit(0)

    # Phase 3: Output
    print(f"\n  ✓ {len(findings)} findings confirmed\n", file=sys.stderr)

    output_format = getattr(args, "output_format", "markdown")
    if output_format == "json":
        import json as json_mod
        print(json_mod.dumps(findings, indent=2, ensure_ascii=False))
    else:
        for i, f in enumerate(findings, 1):
            loc = f.get("location", {})
            print(f"### [{i}] {f.get('pattern', '?')} {f.get('severity', '?').upper()}")
            print(f"**{loc.get('file_a', '?')}** {loc.get('detail_a', '')}")
            if loc.get("file_b"):
                print(f"  ↔ **{loc.get('file_b')}** {loc.get('detail_b', '')}")
            print(f"\n{f.get('contradiction', '')}")
            if f.get("user_impact"):
                print(f"\n**Impact**: {f['user_impact']}")
            print(f"\n_Source: {f.get('internal_evidence', '')}_\n")

    # Auto-record findings to JSONL
    try:
        from findings import add_finding, Finding, generate_id
        # Batch enrich all findings with git data
        try:
            from git_enrichment import enrich_findings_batch
            enrich_findings_batch(findings, repo_path, verbose=verbose)
        except Exception:
            pass
        repo_name = Path(repo_path).name
        recorded = 0
        for f in findings:
            loc = f.get("location", {})
            fid = generate_id(
                repo=repo_name,
                file=loc.get("file_a", ""),
                title=f.get("contradiction", "")[:80],
                file_b=loc.get("file_b", ""),
                pattern=f.get("pattern", ""),
            )
            finding = Finding(
                id=fid,
                repo=repo_name,
                file=loc.get("file_a", ""),
                type="contradiction",
                severity=f.get("severity", "medium"),
                pattern=f.get("pattern", ""),
                title=f.get("contradiction", "")[:120],
                description=f.get("contradiction", ""),
                status="found",
                found_by="deep_scan",
                category=f.get("category", ""),
                taxonomies=f.get("taxonomies"),
                churn_6m=f.get("churn_6m", 0),
                fan_out=f.get("fan_out", 0),
                total_lines=f.get("total_lines", 0),
            )
            try:
                add_finding(repo_path, finding)
                recorded += 1
            except ValueError:
                pass  # duplicate
        if verbose:
            print(f"  Recorded {recorded} findings to .delta-lint/findings/", file=sys.stderr)
    except Exception as e:
        if verbose:
            print(f"  Warning: could not record findings: {e}", file=sys.stderr)

    # Exit code 1 if high-severity findings exist
    high_count = sum(1 for f in findings if f.get("severity") == "high")
    if high_count > 0:
        sys.exit(1)


# ---------------------------------------------------------------------------
# cmd_scan_full (stress-test: virtual modifications × N → landmine map)
# ---------------------------------------------------------------------------

def cmd_scan_full(args):
    """Run full stress-test scan (heavy, 10-30 minutes)."""
    repo_path = str(Path(args.repo).resolve())
    parallel = getattr(args, 'parallel', 10)
    max_wall_time = getattr(args, 'max_wall_time', 2400)

    print("── δ-lint ── フルスキャン（ストレステスト）開始", file=sys.stderr)
    print(f"  仮想改修を生成してスキャンします（並列{parallel}、最大{max_wall_time//60}分）...", file=sys.stderr)
    print(f"  進捗は results.json に逐次保存されます", file=sys.stderr)

    from stress_test import run_stress_test
    run_stress_test(
        repo_path,
        backend=getattr(args, 'backend', 'cli'),
        verbose=True,
        lang=getattr(args, 'lang', 'en'),
        parallel=parallel,
        max_wall_time=max_wall_time,
    )

    # Convert high-risk files to debt findings
    from findings import ingest_stress_test_debt
    added = ingest_stress_test_debt(repo_path)
    if added:
        print(f"\n── δ-lint ── ストレステスト結果から {len(added)}件の技術的負債を登録", file=sys.stderr)


# ---------------------------------------------------------------------------
# cmd_watch (--watch mode)
# ---------------------------------------------------------------------------

def cmd_watch(args):
    """Watch mode: poll for file changes, re-scan on change."""
    import time
    import hashlib

    repo_path = str(Path(args.repo).resolve())
    repo_name = Path(repo_path).name
    interval = getattr(args, 'watch_interval', 3.0)

    def _get_file_snapshot():
        """Get hash of changed file list + mtimes for change detection."""
        try:
            changed = get_changed_files(repo_path, args.diff_target)
            source = filter_source_files(changed)
        except Exception:
            return None, []
        if not source:
            return None, []
        # Hash: file list + mtimes
        parts = []
        for f in sorted(source):
            full = Path(repo_path) / f
            try:
                parts.append(f"{f}:{full.stat().st_mtime_ns}")
            except OSError:
                parts.append(f"{f}:?")
        snap = hashlib.md5("|".join(parts).encode()).hexdigest()
        return snap, source

    print(f"── δ-lint ── Watch mode started", file=sys.stderr)
    print(f"  Repo: {repo_path}", file=sys.stderr)
    print(f"  Interval: {interval}s", file=sys.stderr)
    print(f"  Press Ctrl+C to stop\n", file=sys.stderr)

    last_snapshot = None
    scan_count = 0

    try:
        while True:
            snap, source_files = _get_file_snapshot()

            if snap is None:
                # No changed files — idle
                if last_snapshot is not None:
                    print(f"  ⏸ No changed files — waiting...", file=sys.stderr)
                    last_snapshot = None
                time.sleep(interval)
                continue

            if snap == last_snapshot:
                # No new changes since last scan
                time.sleep(interval)
                continue

            # Changes detected — run scan
            last_snapshot = snap
            scan_count += 1
            ts = time.strftime("%H:%M:%S")

            print(f"\n{'─' * 60}", file=sys.stderr)
            print(f"  🔍 [{ts}] Scan #{scan_count} — "
                  f"{len(source_files)} file(s) changed", file=sys.stderr)
            for f in source_files[:5]:
                print(f"    {f}", file=sys.stderr)
            if len(source_files) > 5:
                print(f"    ... +{len(source_files) - 5} more", file=sys.stderr)
            print(f"{'─' * 60}", file=sys.stderr)

            try:
                # Build context
                _depth = getattr(args, '_depth', 'default')
                _scope = getattr(args, '_scope', 'diff')
                _hops = 3 if (_depth == 'deep' or _scope == 'pr') else 1
                context = build_context(repo_path, source_files, retrieval_config=getattr(args, '_retrieval_config', None), doc_files=getattr(args, '_doc_files', None), max_hops=_hops)
                if not context.target_files:
                    print(f"  ⚠ No readable source files. Skipping.", file=sys.stderr)
                    time.sleep(interval)
                    continue

                # Semantic expansion
                if args.semantic:
                    from semantic import expand_context_semantic
                    context = expand_context_semantic(
                        repo_path, source_files, context,
                        diff_target=args.diff_target,
                        verbose=args.verbose,
                    )

                # Load constraints & policy
                from detector import load_constraints, load_policy
                target_paths = [f.path for f in context.target_files]
                constraints = load_constraints(repo_path, target_paths)
                policy = load_policy(repo_path)
                architecture = policy.get("architecture") if policy else None

                # Diff context
                from retrieval import get_diff_content
                diff_text = get_diff_content(repo_path, args.diff_target)

                # Detect
                findings = detect(
                    context, repo_name=repo_name, model=args.model,
                    backend=args.backend, lang=args.lang,
                    constraints=constraints or None,
                    architecture=architecture,
                    diff_text=diff_text,
                )

                # Verify (Phase 2)
                if not getattr(args, 'no_verify', False) and findings:
                    from verifier import verify_findings as verify
                    findings, _, _ = verify(
                        findings, context,
                        model=args.model, backend=args.backend,
                        verbose=args.verbose,
                    )

                # Filter
                suppressions = load_suppressions(repo_path)
                result = filter_findings(
                    findings, min_severity=args.severity,
                    suppressions=suppressions, repo_path=repo_path,
                )

                # Policy filter
                if policy and (policy.get("accepted") or policy.get("severity_overrides")):
                    from findings import apply_policy
                    result.shown = apply_policy(result.shown, policy)

                # Output
                if result.shown:
                    print(f"\n  ⚡ {len(result.shown)} finding(s) detected:\n",
                          file=sys.stderr)
                    print_results(
                        result.shown,
                        filtered_count=len(result.filtered),
                        suppressed_count=len(result.suppressed),
                        expired_count=len(result.expired),
                        output_format=args.output_format,
                    )
                else:
                    print(f"\n  ✅ No issues found "
                          f"({len(findings)} raw → 0 after filter)\n",
                          file=sys.stderr)

                # Auto-learn sibling relationships
                if not getattr(args, 'no_learn', False) and findings:
                    try:
                        from sibling import update_sibling_map_from_findings
                        new_sibs = update_sibling_map_from_findings(findings, repo_path)
                        if new_sibs and args.verbose:
                            print(f"  Sibling map: +{new_sibs} new",
                                  file=sys.stderr)
                    except Exception:
                        pass

            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"\n  ❌ Scan error: {e}", file=sys.stderr)

            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n\n── δ-lint ── Watch stopped ({scan_count} scan(s) completed)",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# cmd_scan
# ---------------------------------------------------------------------------

def cmd_scan(args):
    """Run structural contradiction scan."""
    # Step 0: Environment pre-check (auto-install missing deps, never exits)
    env = _check_environment(
        backend=getattr(args, "backend", "cli"),
        verbose=getattr(args, "verbose", False),
    )
    # Apply resolved backend (may have changed if claude CLI unavailable)
    if hasattr(args, "backend"):
        args.backend = env["backend"]

    repo_path = str(Path(args.repo).resolve())
    repo_name = Path(repo_path).name

    # Step 1: Identify target files (driven by args._scope)
    scope = getattr(args, '_scope', 'diff')
    if args.files:
        source_files = args.files
        if args.verbose:
            print(f"Scanning {len(source_files)} specified file(s)", file=sys.stderr)
    elif scope == "pr":
        # PR mode: all files changed since merge-base with the base branch
        from retrieval import get_pr_changed_files, _pack_batches
        base_ref = getattr(args, 'base', None)
        pr_files, resolved_base = get_pr_changed_files(repo_path, base_ref)
        if not pr_files:
            print("PR差分が見つかりません。", file=sys.stderr)
            if not resolved_base:
                print("  ベースブランチが検出できません。--base origin/main 等で指定してください。", file=sys.stderr)
            else:
                print(f"  ベースブランチ: {resolved_base} — HEAD との差分が空です。", file=sys.stderr)
            sys.exit(0)
        source_files_pr = filter_source_files(pr_files)
        if not source_files_pr:
            print(f"PR差分 {len(pr_files)} ファイル中、ソースファイルがありません。", file=sys.stderr)
            sys.exit(0)
        batches = _pack_batches(repo_path, source_files_pr)
        if not batches:
            print("No scannable files after batching.", file=sys.stderr)
            sys.exit(0)
        total_files = sum(len(b) for b in batches)
        print(f"  🔀 PR mode: {resolved_base}...HEAD — {total_files} ファイルを {len(batches)} バッチに分割",
              file=sys.stderr)
        for i, batch in enumerate(batches):
            print(f"    Batch {i+1}: {len(batch)} files", file=sys.stderr)
            if args.verbose:
                for f in batch:
                    print(f"      {f}", file=sys.stderr)

        # Run batches in parallel (or sequentially if --parallel=1)
        import subprocess
        import time as _time
        from concurrent.futures import ThreadPoolExecutor, as_completed
        script_path = Path(__file__).resolve()
        all_high = 0
        findings_before = _count_findings_on_disk(repo_path)
        t0 = _time.monotonic()
        parallel = getattr(args, 'parallel', 3)
        dashboard_opened = False

        def run_batch(i, batch):
            """Run a single batch and return (batch_idx, returncode, findings_count)."""
            print(f"\n── Batch {i+1}/{len(batches)} ({len(batch)} files) ──", file=sys.stderr)
            cmd = [
                sys.executable, str(script_path), "scan",
                "--repo", repo_path,
                "--scope", "pr",
                "--files", *batch,
                "--severity", getattr(args, 'severity', 'high'),
                "--lang", getattr(args, 'lang', 'en'),
            ]
            if base_ref:
                cmd.extend(["--base", resolved_base])
            _depth = getattr(args, '_depth', 'default')
            _lens = getattr(args, '_lens', 'default')
            if _depth != "default":
                cmd.extend(["--depth", _depth])
            if _lens != "default":
                cmd.extend(["--lens", _lens])
            _since = getattr(args, 'since', None)
            if _since:
                cmd.extend(["--since", _since])
            if getattr(args, 'verbose', False):
                cmd.append("--verbose")
            if getattr(args, 'no_cache', False):
                cmd.append("--no-cache")
            if getattr(args, 'no_verify', False):
                cmd.append("--no-verify")
            if getattr(args, 'autofix', False):
                cmd.append("--autofix")
            cmd.append("--no-open")  # parent controls browser open
            try:
                result = subprocess.run(cmd, cwd=repo_path, timeout=600)
                returncode = result.returncode
            except subprocess.TimeoutExpired:
                print(f"  [warn] バッチ {i+1}/{len(batches)} がタイムアウト(10min)しました。スキップします。", file=sys.stderr)
                returncode = 0  # don't count timeout as high-severity
            current_findings = _count_findings_on_disk(repo_path)
            return (i, returncode, current_findings)

        if parallel <= 1:
            # Sequential execution
            for i, batch in enumerate(batches):
                batch_idx, returncode, current_findings = run_batch(i, batch)
                if returncode == 1:
                    all_high += 1
                # Open dashboard on first batch that produces findings (early open)
                if not dashboard_opened and current_findings > 0 and not getattr(args, 'no_open', False):
                    _dash = Path(repo_path) / ".delta-lint" / "findings" / "dashboard.html"
                    if _dash.exists():
                        _open_dashboard(str(_dash), live=True)
                        dashboard_opened = True
                        print(f"\n📊 ダッシュボードを開きました（{current_findings}件検出済み）。"
                              f" スキャンはバックグラウンドで継続中...", file=sys.stderr)
                _print_batch_progress(
                    batch_idx, len(batches), len(batch),
                    current_findings, _time.monotonic() - t0,
                    mode_label="PR",
                )
                findings_before = current_findings
        else:
            # Parallel execution
            print(f"  並列実行: {parallel} workers", file=sys.stderr)
            completed = 0
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                futures = {
                    pool.submit(run_batch, i, batch): i
                    for i, batch in enumerate(batches)
                }
                for future in as_completed(futures, timeout=600 * len(batches) + 60):
                    try:
                        batch_idx, returncode, current_findings = future.result(timeout=600)
                        completed += 1
                        if returncode == 1:
                            all_high += 1
                        # Open dashboard on first batch that produces findings
                        if not dashboard_opened and current_findings > 0 and not getattr(args, 'no_open', False):
                            _dash = Path(repo_path) / ".delta-lint" / "findings" / "dashboard.html"
                            if _dash.exists():
                                _open_dashboard(str(_dash), live=True)
                                dashboard_opened = True
                        _print_batch_progress(
                            batch_idx, len(batches), len(batches[batch_idx]),
                            current_findings, _time.monotonic() - t0,
                            mode_label="PR",
                        )
                    except Exception as exc:
                        completed += 1
                        batch_idx = futures[future]
                        print(f"  [warn] バッチ {batch_idx+1}/{len(batches)} でエラー: {exc}", file=sys.stderr)
        elapsed_total = _time.monotonic() - t0
        final_findings = _count_findings_on_disk(repo_path)
        new_findings = final_findings - findings_before
        print(
            f"\n── PR scan 完了: {resolved_base}...HEAD │ {len(batches)} バッチ │ "
            f"{final_findings} findings │ {_format_elapsed(elapsed_total)} ──",
            file=sys.stderr,
        )
        if new_findings > 0:
            try:
                from findings import append_scan_history
                append_scan_history(
                    repo_path,
                    clusters=total_files,
                    findings_count=new_findings,
                    scan_type="existing",
                    scope="pr",
                    depth=getattr(args, '_depth', 'default'),
                    lens=getattr(args, '_lens', 'default'),
                )
            except Exception:
                pass
        sys.exit(1 if all_high > 0 else 0)
    elif scope == "wide":
        # Wide mode: all source files, batched like smart mode
        from surface_extractor import collect_all_source_files
        from retrieval import _pack_batches
        all_files = collect_all_source_files(repo_path)
        if not all_files:
            print("No source files found in repository.", file=sys.stderr)
            sys.exit(0)
        batches = _pack_batches(repo_path, all_files)
        if not batches:
            print("No scannable files after batching.", file=sys.stderr)
            sys.exit(0)
        total_files = sum(len(b) for b in batches)
        print(f"  🔭 Wide mode: {total_files} ファイルを {len(batches)} バッチに分割", file=sys.stderr)
        for i, batch in enumerate(batches):
            print(f"    Batch {i+1}: {len(batch)} files", file=sys.stderr)
            if args.verbose:
                for f in batch:
                    print(f"      {f}", file=sys.stderr)

        # Run batches in parallel (or sequentially if --parallel=1)
        import subprocess
        import time as _time
        from concurrent.futures import ThreadPoolExecutor, as_completed
        script_path = Path(__file__).resolve()
        all_high = 0
        findings_before = _count_findings_on_disk(repo_path)
        t0 = _time.monotonic()
        parallel = getattr(args, 'parallel', 3)
        dashboard_opened = False

        def run_batch(i, batch):
            """Run a single batch and return (batch_idx, returncode, findings_count)."""
            print(f"\n── Batch {i+1}/{len(batches)} ({len(batch)} files) ──", file=sys.stderr)
            cmd = [
                sys.executable, str(script_path), "scan",
                "--repo", repo_path,
                "--scope", "wide",
                "--files", *batch,
                "--severity", getattr(args, 'severity', 'high'),
                "--lang", getattr(args, 'lang', 'en'),
            ]
            _depth = getattr(args, '_depth', 'default')
            _lens = getattr(args, '_lens', 'default')
            if _depth != "default":
                cmd.extend(["--depth", _depth])
            if _lens != "default":
                cmd.extend(["--lens", _lens])
            _since = getattr(args, 'since', None)
            if _since:
                cmd.extend(["--since", _since])
            if getattr(args, 'verbose', False):
                cmd.append("--verbose")
            if getattr(args, 'no_cache', False):
                cmd.append("--no-cache")
            if getattr(args, 'no_verify', False):
                cmd.append("--no-verify")
            if getattr(args, 'autofix', False):
                cmd.append("--autofix")
            cmd.append("--no-open")  # parent controls browser open
            try:
                result = subprocess.run(cmd, cwd=repo_path, timeout=600)
                returncode = result.returncode
            except subprocess.TimeoutExpired:
                print(f"  [warn] バッチ {i+1}/{len(batches)} がタイムアウト(10min)しました。スキップします。", file=sys.stderr)
                returncode = 0  # don't count timeout as high-severity
            current_findings = _count_findings_on_disk(repo_path)
            return (i, returncode, current_findings)

        if parallel <= 1:
            # Sequential execution
            for i, batch in enumerate(batches):
                batch_idx, returncode, current_findings = run_batch(i, batch)
                if returncode == 1:
                    all_high += 1
                # Open dashboard on first batch that produces findings (early open)
                if not dashboard_opened and current_findings > 0 and not getattr(args, 'no_open', False):
                    _dash = Path(repo_path) / ".delta-lint" / "findings" / "dashboard.html"
                    if _dash.exists():
                        _open_dashboard(str(_dash), live=True)
                        dashboard_opened = True
                        print(f"\n📊 ダッシュボードを開きました（{current_findings}件検出済み）。"
                              f" スキャンはバックグラウンドで継続中...", file=sys.stderr)
                new_in_batch = current_findings - findings_before
                _print_batch_progress(
                    batch_idx, len(batches), len(batch),
                    current_findings, _time.monotonic() - t0,
                    mode_label="Wide",
                )
                if new_in_batch > findings_before:
                    findings_before = current_findings
        else:
            # Parallel execution
            print(f"  並列実行: {parallel} workers", file=sys.stderr)
            completed = 0
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                futures = {
                    pool.submit(run_batch, i, batch): i
                    for i, batch in enumerate(batches)
                }
                for future in as_completed(futures, timeout=600 * len(batches) + 60):
                    try:
                        batch_idx, returncode, current_findings = future.result(timeout=600)
                        completed += 1
                        if returncode == 1:
                            all_high += 1
                        # Open dashboard on first batch that produces findings
                        if not dashboard_opened and current_findings > 0 and not getattr(args, 'no_open', False):
                            _dash = Path(repo_path) / ".delta-lint" / "findings" / "dashboard.html"
                            if _dash.exists():
                                _open_dashboard(str(_dash), live=True)
                                dashboard_opened = True
                        _print_batch_progress(
                            batch_idx, len(batches), len(batches[batch_idx]),
                            current_findings, _time.monotonic() - t0,
                            mode_label="Wide",
                        )
                    except Exception as exc:
                        completed += 1
                        batch_idx = futures[future]
                        print(f"  [warn] バッチ {batch_idx+1}/{len(batches)} でエラー: {exc}", file=sys.stderr)
        elapsed_total = _time.monotonic() - t0
        final_findings = _count_findings_on_disk(repo_path)
        new_findings = final_findings - findings_before
        print(
            f"\n── Wide scan 完了: {len(batches)} バッチ │ "
            f"{final_findings} findings │ {_format_elapsed(elapsed_total)} ──",
            file=sys.stderr,
        )
        if new_findings > 0:
            try:
                from findings import append_scan_history
                append_scan_history(
                    repo_path,
                    clusters=sum(len(b) for b in batches),
                    findings_count=new_findings,
                    scan_type="existing",
                    scope="wide",
                    depth=getattr(args, '_depth', 'default'),
                    lens=getattr(args, '_lens', 'default'),
                )
            except Exception:
                pass
        sys.exit(1 if all_high > 0 else 0)
    elif scope == "smart" or getattr(args, 'smart', False):
        # Smart mode: select files by git history priority with batching
        # Run each batch as a separate subprocess with --files
        from retrieval import get_priority_batches
        batches = get_priority_batches(repo_path)
        if not batches:
            print("No priority files found from git history. Use --files to specify files manually.",
                  file=sys.stderr)
            sys.exit(0)
        total_files = sum(len(b) for b in batches)
        print(f"  🎯 Smart mode: git history から {total_files} ファイルを {len(batches)} バッチに分割", file=sys.stderr)
        for i, batch in enumerate(batches):
            print(f"    Batch {i+1}: {len(batch)} files", file=sys.stderr)
            if args.verbose:
                for f in batch:
                    print(f"      {f}", file=sys.stderr)

        # Run batches in parallel (or sequentially if --parallel=1)
        import subprocess
        import time as _time
        from concurrent.futures import ThreadPoolExecutor, as_completed
        script_path = Path(__file__).resolve()
        all_high = 0
        findings_before = _count_findings_on_disk(repo_path)
        t0 = _time.monotonic()
        parallel = getattr(args, 'parallel', 3)
        dashboard_opened = False

        def run_batch(i, batch):
            """Run a single batch and return (batch_idx, returncode, findings_count)."""
            print(f"\n── Batch {i+1}/{len(batches)} ({len(batch)} files) ──", file=sys.stderr)
            cmd = [
                sys.executable, str(script_path), "scan",
                "--repo", repo_path,
                "--scope", "smart",
                "--files", *batch,
                "--severity", getattr(args, 'severity', 'high'),
                "--lang", getattr(args, 'lang', 'en'),
            ]
            _depth = getattr(args, '_depth', 'default')
            _lens = getattr(args, '_lens', 'default')
            if _depth != "default":
                cmd.extend(["--depth", _depth])
            if _lens != "default":
                cmd.extend(["--lens", _lens])
            _since = getattr(args, 'since', None)
            if _since:
                cmd.extend(["--since", _since])
            if getattr(args, 'verbose', False):
                cmd.append("--verbose")
            if getattr(args, 'no_cache', False):
                cmd.append("--no-cache")
            if getattr(args, 'no_verify', False):
                cmd.append("--no-verify")
            if getattr(args, 'autofix', False):
                cmd.append("--autofix")
            cmd.append("--no-open")  # parent controls browser open
            try:
                result = subprocess.run(cmd, cwd=repo_path, timeout=600)
                returncode = result.returncode
            except subprocess.TimeoutExpired:
                print(f"  [warn] バッチ {i+1}/{len(batches)} がタイムアウト(10min)しました。スキップします。", file=sys.stderr)
                returncode = 0  # don't count timeout as high-severity
            current_findings = _count_findings_on_disk(repo_path)
            return (i, returncode, current_findings)

        if parallel <= 1:
            # Sequential execution
            for i, batch in enumerate(batches):
                batch_idx, returncode, current_findings = run_batch(i, batch)
                if returncode == 1:
                    all_high += 1
                # Open dashboard on first batch that produces findings (early open)
                if not dashboard_opened and current_findings > 0 and not getattr(args, 'no_open', False):
                    _dash = Path(repo_path) / ".delta-lint" / "findings" / "dashboard.html"
                    if _dash.exists():
                        _open_dashboard(str(_dash), live=True)
                        dashboard_opened = True
                        print(f"\n📊 ダッシュボードを開きました（{current_findings}件検出済み）。"
                              f" スキャンはバックグラウンドで継続中...", file=sys.stderr)
                new_in_batch = current_findings - findings_before
                _print_batch_progress(
                    batch_idx, len(batches), len(batch),
                    current_findings, _time.monotonic() - t0,
                    mode_label="Smart",
                )
                if new_in_batch > findings_before:
                    findings_before = current_findings
        else:
            # Parallel execution
            print(f"  並列実行: {parallel} workers", file=sys.stderr)
            completed = 0
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                futures = {
                    pool.submit(run_batch, i, batch): i
                    for i, batch in enumerate(batches)
                }
                for future in as_completed(futures, timeout=600 * len(batches) + 60):
                    try:
                        batch_idx, returncode, current_findings = future.result(timeout=600)
                        completed += 1
                        if returncode == 1:
                            all_high += 1
                        # Open dashboard on first batch that produces findings
                        if not dashboard_opened and current_findings > 0 and not getattr(args, 'no_open', False):
                            _dash = Path(repo_path) / ".delta-lint" / "findings" / "dashboard.html"
                            if _dash.exists():
                                _open_dashboard(str(_dash), live=True)
                                dashboard_opened = True
                        _print_batch_progress(
                            batch_idx, len(batches), len(batches[batch_idx]),
                            current_findings, _time.monotonic() - t0,
                            mode_label="Smart",
                        )
                    except Exception as exc:
                        completed += 1
                        batch_idx = futures[future]
                        print(f"  [warn] バッチ {batch_idx+1}/{len(batches)} でエラー: {exc}", file=sys.stderr)
        elapsed_total = _time.monotonic() - t0
        final_findings = _count_findings_on_disk(repo_path)
        new_findings = final_findings - findings_before
        print(
            f"\n── Smart scan 完了: {len(batches)} バッチ │ "
            f"{final_findings} findings │ {_format_elapsed(elapsed_total)} ──",
            file=sys.stderr,
        )
        if new_findings > 0:
            try:
                from findings import append_scan_history
                append_scan_history(
                    repo_path,
                    clusters=sum(len(b) for b in batches),
                    findings_count=new_findings,
                    scan_type="existing",
                    scope="smart",
                    depth=getattr(args, '_depth', 'default'),
                    lens=getattr(args, '_lens', 'default'),
                )
            except Exception:
                pass
        sys.exit(1 if all_high > 0 else 0)
    else:
        explicit_since = getattr(args, 'since', None)
        since_val = explicit_since or _adaptive_since(repo_path, verbose=args.verbose)
        if args.verbose:
            print(f"Detecting changed files in {repo_path} (since={since_val})...", file=sys.stderr)
        from retrieval import get_recent_changed_files
        all_changed = get_recent_changed_files(repo_path, since=since_val)
        source_files = filter_source_files(all_changed)

        if not source_files:
            print(f"直近 {since_val} に変更されたソースファイルがありません。"
                  " --files や --scope smart をお試しください。",
                  file=sys.stderr)
            sys.exit(0)

        print(f"  📅 diff mode (since={since_val}): {len(source_files)} ファイル検出", file=sys.stderr)
        if args.verbose:
            for f in source_files:
                print(f"    {f}", file=sys.stderr)

    # Step 1.5: Resolve document files (--docs)
    if hasattr(args, "docs") and args.docs is not None:
        if args.docs:  # explicit paths given
            args._doc_files = args.docs
        else:  # --docs with no arguments → auto-discover
            args._doc_files = _auto_discover_docs(repo_path)
        if args.verbose and getattr(args, '_doc_files', None):
            print(f"Document contract surfaces: {len(args._doc_files)}", file=sys.stderr)
            for d in args._doc_files:
                print(f"  {d}", file=sys.stderr)

    # Step 2: Build context
    print(f"Building module context...", file=sys.stderr)

    _depth = getattr(args, '_depth', 'default')
    _scope = getattr(args, '_scope', 'diff')
    _hops = 3 if (_depth == 'deep' or _scope == 'pr') else 1
    context = build_context(repo_path, source_files, retrieval_config=getattr(args, '_retrieval_config', None), doc_files=getattr(args, '_doc_files', None), max_hops=_hops)

    # Always show context summary (important progress info)
    print(f"  Target files: {len(context.target_files)}", file=sys.stderr)
    print(f"  Dependency files: {len(context.dep_files)}", file=sys.stderr)
    if context.doc_files:
        print(f"  Document files: {len(context.doc_files)}", file=sys.stderr)
    print(f"  Total context: {context.total_chars} chars", file=sys.stderr)
    # Warnings always shown (important)
    for w in context.warnings:
        print(f"  WARNING: {w}", file=sys.stderr)

    if not context.target_files:
        print("No readable source files in context. Nothing to scan.", file=sys.stderr)
        sys.exit(0)

    # Step 2.5: Semantic expansion (--semantic)
    if args.semantic:
        from semantic import expand_context_semantic
        context = expand_context_semantic(
            repo_path, source_files, context,
            diff_target=args.diff_target,
            verbose=args.verbose,
        )

    # Step 3: Dry run - show context and exit
    if args.dry_run:
        print("=== DRY RUN: Context that would be sent to LLM ===\n", file=sys.stderr)
        print(f"Target files ({len(context.target_files)}):", file=sys.stderr)
        for f in context.target_files:
            print(f"  {f.path} ({len(f.content)} chars)", file=sys.stderr)
        print(f"Dependency files ({len(context.dep_files)}):", file=sys.stderr)
        for f in context.dep_files:
            print(f"  {f.path} ({len(f.content)} chars)", file=sys.stderr)
        print(f"\nTotal: {context.total_chars} chars", file=sys.stderr)
        if context.warnings:
            print(f"\nWarnings:", file=sys.stderr)
            for w in context.warnings:
                print(f"  {w}", file=sys.stderr)
        sys.exit(0)

    # Step 3.5: Load known constraints, team policy, and config
    from detector import load_constraints, load_policy
    config = _load_config(repo_path)
    target_paths = [f.path for f in context.target_files]
    constraints = load_constraints(repo_path, target_paths)
    policy = load_policy(repo_path)

    # Merge profile policy into constraints.yml policy (profile wins on conflict)
    profile_policy = getattr(args, '_profile_policy', None)
    if profile_policy:
        if not policy:
            policy = {}
        # prompt_append: concatenate (profile appends to constraints.yml)
        if "prompt_append" in profile_policy:
            existing = policy.get("prompt_append", "")
            policy["prompt_append"] = (existing + "\n\n" + profile_policy["prompt_append"]).strip()
        # disabled_patterns: profile overrides config.json
        if "disabled_patterns" in profile_policy:
            config["disabled_patterns"] = profile_policy["disabled_patterns"]
        # detect_prompt: custom detection prompt path or inline
        # - File path (relative to repo): loaded and used as system prompt
        # - Inline string: used directly as system prompt
        if "detect_prompt" in profile_policy:
            policy["detect_prompt"] = profile_policy["detect_prompt"]
        # accepted: accepted rules override (per-pattern or per-file exceptions)
        if "accepted" in profile_policy:
            policy["accepted"] = profile_policy["accepted"]
        # severity_overrides: per-pattern severity remapping
        if "severity_overrides" in profile_policy:
            policy["severity_overrides"] = profile_policy["severity_overrides"]
        # debt_budget: max active debt score threshold for CI gate
        if "debt_budget" in profile_policy:
            policy["debt_budget"] = profile_policy["debt_budget"]
        # scoring_weights: override scoring formula weights
        if "scoring_weights" in profile_policy:
            policy["scoring_weights"] = profile_policy["scoring_weights"]
        # dashboard_template: custom findings dashboard HTML template
        if "dashboard_template" in profile_policy:
            args._dashboard_template = profile_policy["dashboard_template"]
        # docs: enable document contract surface checking from profile
        if "docs" in profile_policy and not getattr(args, '_doc_files', None):
            doc_val = profile_policy["docs"]
            if doc_val is True:
                args._doc_files = _auto_discover_docs(repo_path)
            elif isinstance(doc_val, list):
                args._doc_files = doc_val
        # Other policy keys: profile overrides
        for k in ("architecture", "project_rules", "exclude_paths"):
            if k in profile_policy:
                policy[k] = profile_policy[k]
        if args.verbose:
            print(f"  Profile policy merged: {list(profile_policy.keys())}", file=sys.stderr)

    if constraints and args.verbose:
        total_c = sum(len(c.get("implicit_constraints", [])) for c in constraints)
        print(f"  Loaded {total_c} constraint(s) from {len(constraints)} module(s)", file=sys.stderr)
    # Apply exclude_paths from policy (filter out 3rd-party / vendor code)
    exclude_paths = policy.get("exclude_paths", []) if policy else []
    if exclude_paths:
        import fnmatch
        before_count = len(source_files)
        source_files = [
            f for f in source_files
            if not any(fnmatch.fnmatch(f, pat) for pat in exclude_paths)
        ]
        excluded_count = before_count - len(source_files)
        if excluded_count > 0:
            # Rebuild context without excluded files
            context = build_context(repo_path, source_files, retrieval_config=getattr(args, '_retrieval_config', None), doc_files=getattr(args, '_doc_files', None), max_hops=_hops)
            if args.verbose:
                print(f"  Excluded {excluded_count} file(s) by policy exclude_paths", file=sys.stderr)

    if policy and args.verbose:
        parts = []
        if policy.get("architecture"):
            parts.append(f"{len(policy['architecture'])} architecture context(s)")
        if policy.get("project_rules"):
            parts.append(f"{len(policy['project_rules'])} project rule(s)")
        if policy.get("exclude_paths"):
            parts.append(f"{len(policy['exclude_paths'])} exclude path(s)")
        if policy.get("accepted"):
            parts.append(f"{len(policy['accepted'])} accepted rule(s)")
        if policy.get("severity_overrides"):
            parts.append(f"{len(policy['severity_overrides'])} severity override(s)")
        if policy.get("prompt_append"):
            parts.append("prompt_append")
        if policy.get("debt_budget") is not None:
            parts.append(f"debt_budget={policy['debt_budget']}")
        if parts:
            print(f"  Policy: {', '.join(parts)}", file=sys.stderr)

    if config.get("disabled_patterns") and args.verbose:
        print(f"  Disabled patterns: {', '.join(config['disabled_patterns'])}", file=sys.stderr)

    # Step 3.7: Get git diff for change-aware detection
    from retrieval import get_diff_content
    diff_text = ""
    if scope == "pr":
        from retrieval import get_pr_diff_content
        diff_text = get_pr_diff_content(repo_path, getattr(args, 'base', None))
    elif not args.files:
        diff_text = get_diff_content(repo_path, args.diff_target)
        if args.verbose and diff_text:
            print(f"  Diff context: {len(diff_text)} chars", file=sys.stderr)

    # Step 3.9: Check cache (skip LLM if same context was scanned before)
    from cache import compute_context_hash, get_cached_findings, save_cached_findings
    context_hash = compute_context_hash(context.target_files, context.dep_files)
    cache_hit = False

    if not getattr(args, 'no_cache', False):
        cached = get_cached_findings(repo_path, context_hash)
        if cached is not None:
            findings = cached
            cache_hit = True
            # Always show cache hit (important to know why it's fast)
            print(f"  Cache hit ({context_hash[:8]}...) — {len(findings)} finding(s)",
                  file=sys.stderr)

    # Step 4: Run detection (skip if cache hit)
    if not cache_hit:
        print(f"Running detection with {args.model}...", file=sys.stderr)

        architecture = policy.get("architecture") if policy else None
        project_rules = policy.get("project_rules") if policy else None
        prompt_append = policy.get("prompt_append", "") if policy else ""
        disabled_patterns = config.get("disabled_patterns") if config else None

        # detect_prompt: profile can override the entire detection prompt
        # Value can be a file path (relative to repo) or inline prompt text
        detect_prompt_override = ""
        raw_detect_prompt = policy.get("detect_prompt", "") if policy else ""
        if raw_detect_prompt:
            # If it looks like a file path, try to load it
            prompt_file = Path(repo_path) / raw_detect_prompt
            if prompt_file.exists() and prompt_file.is_file():
                detect_prompt_override = prompt_file.read_text(encoding="utf-8")
                if args.verbose:
                    print(f"  Custom detect prompt: {raw_detect_prompt}", file=sys.stderr)
            else:
                # Treat as inline prompt text
                detect_prompt_override = raw_detect_prompt
                if args.verbose:
                    print(f"  Custom detect prompt: inline ({len(raw_detect_prompt)} chars)", file=sys.stderr)

        findings = detect(context, repo_name=repo_name, model=args.model,
                           backend=args.backend, lang=args.lang,
                           constraints=constraints or None,
                           architecture=architecture,
                           diff_text=diff_text,
                           project_rules=project_rules,
                           repo_path=repo_path,
                           prompt_append=prompt_append,
                           disabled_patterns=disabled_patterns,
                           detect_prompt=detect_prompt_override,
                           lens=getattr(args, '_lens', 'default'))

        print(f"  Raw findings: {len(findings)}", file=sys.stderr)

    # Step 4.2: Verify findings (Phase 2 — reject false positives)
    verification_meta = None
    rejected_findings = []
    if not getattr(args, 'no_verify', False) and findings:
        print(f"Verifying {len(findings)} finding(s)...", file=sys.stderr)
        from verifier import verify_findings as verify
        findings, rejected_findings, verification_meta = verify(
            findings, context,
            model=args.model, backend=args.backend,
            verbose=args.verbose,
        )
        # Always show verification results (important progress info)
        if verification_meta:
            print(f"  Verified: {verification_meta['confirmed']} confirmed, "
                  f"{verification_meta['rejected']} rejected", file=sys.stderr)

    # Step 4.3: Save to cache (after verification, so cache includes verified results)
    if not cache_hit and not getattr(args, 'no_cache', False):
        save_cached_findings(repo_path, context_hash, findings, model=args.model)
        if args.verbose:
            print(f"  Cached results ({context_hash})", file=sys.stderr)

    # Step 4.5: Load suppressions
    suppressions = load_suppressions(repo_path)
    if args.verbose and suppressions:
        print(f"  Loaded {len(suppressions)} suppress entry(ies)", file=sys.stderr)

    # Step 5: Filter and output (with suppress support)
    result = filter_findings(findings, min_severity=args.severity,
                             suppressions=suppressions, repo_path=repo_path)

    # Step 5.1: Apply team policy (accepted filter + severity overrides)
    policy_filtered = 0
    if policy and (policy.get("accepted") or policy.get("severity_overrides")):
        from findings import apply_policy
        before = len(result.shown)
        result.shown = apply_policy(result.shown, policy)
        policy_filtered = before - len(result.shown)

    # Step 5.15: Apply category severity boost
    config = _load_config(repo_path)
    categories = config.get("categories", {})
    if categories:
        _apply_category_severity_boost(result.shown, categories, verbose=args.verbose)
        # Re-filter: boosted findings may now fall below min_severity
        from output import SEVERITY_ORDER
        threshold = SEVERITY_ORDER.get(args.severity, 0)
        before_cat = len(result.shown)
        result.shown = [f for f in result.shown
                        if SEVERITY_ORDER.get(f.get("severity", "medium").lower(), 1) <= threshold]
        cat_filtered = before_cat - len(result.shown)
        if cat_filtered and args.verbose:
            print(f"  Category filtered: {cat_filtered}", file=sys.stderr)

    # Step 5.2: diff-only filter (keep only findings touching changed files)
    diff_only_filtered = 0
    if getattr(args, 'diff_only', False) and not args.files:
        from output import filter_diff_only
        before = len(result.shown)
        result.shown = filter_diff_only(result.shown, source_files)
        diff_only_filtered = before - len(result.shown)

    # Always show basic filtering results (important progress info)
    print(f"  Shown (>= {args.severity}): {len(result.shown)}", file=sys.stderr)
    if len(result.filtered) > 0:
        print(f"  Filtered: {len(result.filtered)}", file=sys.stderr)
    # Detailed breakdown only with --verbose
    if args.verbose:
        if diff_only_filtered:
            print(f"  Diff-only filtered: {diff_only_filtered}", file=sys.stderr)
        if policy_filtered:
            print(f"  Policy accepted: {policy_filtered}", file=sys.stderr)
        if result.suppressed:
            print(f"  Suppressed: {len(result.suppressed)}", file=sys.stderr)
        if result.expired:
            print(f"  Expired: {len(result.expired)}", file=sys.stderr)

    # Report expired suppressions as warnings
    for entry in result.expired_entries:
        files_str = " <-> ".join(entry.files)
        print(f"WARNING: suppress {entry.id} expired (code changed): {files_str}",
              file=sys.stderr)

    # Resolve persona: explicit --for > config.json > "engineer"
    persona = args.persona
    if persona is None:
        from persona_translator import load_default_persona
        persona = load_default_persona(repo_path)

    if persona in ("pm", "qa"):
        # Non-engineer mode: translate findings instead of technical output
        from persona_translator import translate
        translated = translate(result.shown, persona=persona,
                               model=args.model, verbose=args.verbose)
        if translated:
            print(translated)
        else:
            # Fallback: show standard output if translation failed
            print_results(result.shown,
                          filtered_count=len(result.filtered),
                          suppressed_count=len(result.suppressed),
                          expired_count=len(result.expired),
                          output_format=args.output_format)
    else:
        print_results(result.shown,
                      filtered_count=len(result.filtered),
                      suppressed_count=len(result.suppressed),
                      expired_count=len(result.expired),
                      output_format=args.output_format)

    # Step 5.5: Persist findings to JSONL + refresh dashboard HTML (early open for first few findings)
    # (Terminal output alone does not feed delta view — findings_dashboard reads *.jsonl.)
    recorded_findings = 0
    dashboard_opened_early = False
    EARLY_DASHBOARD_THRESHOLD = 3  # Open dashboard after first 3 findings
    if result.shown:
        try:
            from findings import add_finding, Finding, generate_id, generate_dashboard
            try:
                from git_enrichment import enrich_findings_batch
                enrich_findings_batch(result.shown, repo_path, verbose=args.verbose)
            except Exception:
                pass
            for f in result.shown:
                loc = f.get("location", {})
                full_title = f.get("contradiction") or f.get("title") or ""
                fid = generate_id(
                    repo_name,
                    loc.get("file_a", ""),
                    full_title[:120],
                    file_b=loc.get("file_b", ""),
                    pattern=f.get("pattern", ""),
                )
                desc = f.get("contradiction") or f.get("user_impact") or f.get("impact") or ""
                finding = Finding(
                    id=fid,
                    repo=repo_name,
                    file=loc.get("file_a", ""),
                    type="contradiction",
                    severity=f.get("severity", "medium"),
                    pattern=f.get("pattern", ""),
                    title=full_title,
                    description=desc,
                    status="found",
                    found_by="scan",
                    category=f.get("category", ""),
                    taxonomies=f.get("taxonomies"),
                    churn_6m=f.get("churn_6m", 0),
                    fan_out=f.get("fan_out", 0),
                    total_lines=f.get("total_lines", 0),
                    contradiction=f.get("contradiction", ""),
                    impact=f.get("impact", ""),
                    user_impact=f.get("user_impact", ""),
                    internal_evidence=f.get("internal_evidence", ""),
                    file_b=loc.get("file_b", ""),
                )
                try:
                    add_finding(repo_path, finding)
                    recorded_findings += 1

                    # Early dashboard open: after first few findings, open dashboard and continue in background
                    if (not dashboard_opened_early and recorded_findings >= EARLY_DASHBOARD_THRESHOLD
                            and not getattr(args, 'no_open', False)):
                        _treemap = None
                        _results_json = Path(repo_path) / ".delta-lint" / "stress-test" / "results.json"
                        if _results_json.exists():
                            try:
                                from visualize import build_treemap_json
                                _treemap = build_treemap_json(str(_results_json))
                            except Exception:
                                pass
                        _dash_tpl = getattr(args, "_dashboard_template", "")
                        dash_path = generate_dashboard(repo_path, treemap_json=_treemap, dashboard_template=_dash_tpl)
                        if dash_path:
                            _open_dashboard(str(dash_path), live=True)
                            dashboard_opened_early = True
                            print(f"\n📊 ダッシュボードを開きました（{recorded_findings}件検出済み）。"
                                  f" スキャンはバックグラウンドで継続中...", file=sys.stderr)
                except ValueError:
                    pass  # duplicate
                except OSError as e:
                    print(f"  [warn] finding保存失敗: {e}", file=sys.stderr)

            # Final dashboard update (if not opened early, or refresh if opened early)
            if recorded_findings > 0:
                _treemap = None
                _results_json = Path(repo_path) / ".delta-lint" / "stress-test" / "results.json"
                if _results_json.exists():
                    try:
                        from visualize import build_treemap_json
                        _treemap = build_treemap_json(str(_results_json))
                    except Exception:
                        pass
                _dash_tpl = getattr(args, "_dashboard_template", "")
                dash_path = generate_dashboard(repo_path, treemap_json=_treemap, dashboard_template=_dash_tpl)
                if dash_path and not dashboard_opened_early and not getattr(args, 'no_open', False):
                    _open_dashboard(str(dash_path), live=True)
        except Exception as e:
            if args.verbose:
                print(f"  [warn] findings保存中にエラー: {e}", file=sys.stderr)

    # Always open dashboard after scan if findings exist on disk (even if no new ones this run)
    if not dashboard_opened_early and recorded_findings == 0 and not getattr(args, 'no_open', False):
        try:
            from findings import list_findings, generate_dashboard
            existing = list_findings(repo_path)
            if existing:
                _dash_tpl = getattr(args, "_dashboard_template", "")
                dash_path = generate_dashboard(repo_path, dashboard_template=_dash_tpl)
                if dash_path:
                    _open_dashboard(str(dash_path), live=True)
        except Exception:
            pass

    # Step 5.6: Autofix (generate + apply locally)
    if getattr(args, 'autofix', False) and result.shown:
        from fixgen import generate_fixes, apply_fixes_locally
        print(f"\n── Autofix: generating fixes for {len(result.shown)} finding(s)...",
              file=sys.stderr)
        fixes = generate_fixes(
            result.shown, context,
            model=args.model, backend=args.backend,
            verbose=args.verbose,
        )
        if fixes:
            applied = apply_fixes_locally(fixes, repo_path, verbose=args.verbose)
            if applied:
                print(f"\n✅ Applied {len(applied)} fix(es):", file=sys.stderr)
                for fix in applied:
                    explanation = fix.get("explanation", "")
                    print(f"  {fix.get('file', '?')}:{fix.get('line', '?')} — {explanation}",
                          file=sys.stderr)
            else:
                print("\n⚠ Fixes generated but none could be applied (old_code mismatch).",
                      file=sys.stderr)
        else:
            print("\n⚠ No fixes could be generated.", file=sys.stderr)

    # Step 6: Save log
    log_dir = args.log_dir or str(Path(repo_path) / ".delta-lint")
    context_meta = {
        "repo": repo_name,
        "repo_path": repo_path,
        "target_files": [f.path for f in context.target_files],
        "dep_files": [f.path for f in context.dep_files],
        "total_chars": context.total_chars,
        "model": args.model,
        "severity_filter": args.severity,
        "warnings": context.warnings,
    }
    if verification_meta:
        context_meta["verification"] = verification_meta
    if rejected_findings:
        context_meta["rejected_findings"] = rejected_findings
    log_path = save_log(result, context_meta, log_dir)
    if args.verbose:
        print(f"\nFull log saved to {log_path}", file=sys.stderr)

    # Step 6.3: Record scan history (with finding_ids for Chao1 coverage estimation)
    # Skip for batch child processes (--files with batched scope) — parent records once.
    is_batch_child = getattr(args, 'files', None) and scope in ("smart", "wide", "pr")
    try:
        from findings import append_scan_history, generate_id
        scan_fids = []
        scan_patterns = []
        for f in result.shown:
            loc = f.get("location", {})
            fid = generate_id(
                repo_name, loc.get("file_a", ""),
                f.get("contradiction", f.get("title", ""))[:120],
                file_b=loc.get("file_b", ""), pattern=f.get("pattern", ""),
            )
            scan_fids.append(fid)
            if f.get("pattern"):
                scan_patterns.append(f["pattern"])
        if not is_batch_child:
            _scan_type = "diff"
            if scope == "smart":
                _scan_type = "existing"
            elif scope == "wide":
                _scan_type = "deep" if getattr(args, '_depth', 'default') == "deep" else "existing"
            elif scope == "pr":
                _scan_type = "existing"
            append_scan_history(
                repo_path,
                clusters=len(context.target_files),
                findings_count=len(result.shown),
                scan_type=_scan_type,
                finding_ids=scan_fids,
                patterns_found=scan_patterns,
                scope=scope,
                depth=getattr(args, '_depth', 'default'),
                lens=getattr(args, '_lens', 'default'),
            )
        from findings import generate_dashboard as _gen_dash
        _treemap = None
        _rj = Path(repo_path) / ".delta-lint" / "stress-test" / "results.json"
        if _rj.exists():
            try:
                from visualize import build_treemap_json
                _treemap = build_treemap_json(str(_rj))
            except Exception:
                pass
        _gen_dash(repo_path, treemap_json=_treemap,
                  dashboard_template=getattr(args, "_dashboard_template", ""))
    except Exception:
        pass

    # Step 6.5: Update sibling map (auto-learn from findings)
    if not getattr(args, 'no_learn', False) and findings:
        try:
            from sibling import update_sibling_map_from_findings
            new_siblings = update_sibling_map_from_findings(findings, repo_path)
            if new_siblings and args.verbose:
                print(f"  Sibling map: +{new_siblings} new relationship(s)", file=sys.stderr)
        except Exception:
            pass  # Non-critical — don't fail scan for sibling map errors

    # Step 6.7: Save baseline snapshot (--baseline-save)
    if getattr(args, 'baseline_save', False):
        all_findings = result.shown + result.filtered + result.suppressed
        snap = _save_baseline_snapshot(repo_path, all_findings, verbose=args.verbose)
        if snap:
            print(f"\n✅ Baseline snapshot saved: {snap}", file=sys.stderr)

    # Step 7: Baseline comparison (--baseline)
    baseline_ref = getattr(args, 'baseline', None)
    if baseline_ref and result.shown:
        baseline_hashes = _build_baseline_hashes(repo_path, baseline_ref, verbose=args.verbose)
        if baseline_hashes is not None:
            new_findings, baseline_count = _filter_new_findings(result.shown, baseline_hashes)
            if args.verbose or baseline_count > 0:
                print(f"\n  Baseline comparison vs {baseline_ref}:", file=sys.stderr)
                print(f"    Known (in baseline): {baseline_count}", file=sys.stderr)
                print(f"    New: {len(new_findings)}", file=sys.stderr)
            # Only new findings determine exit code
            high_count = sum(1 for f in new_findings if f.get("severity", "").lower() == "high")
            if new_findings:
                print(f"\n⚠ {len(new_findings)} new finding(s) since {baseline_ref}"
                      f" ({high_count} high)", file=sys.stderr)
            else:
                print(f"\n✅ No new findings since {baseline_ref}"
                      f" ({baseline_count} existing, all known)", file=sys.stderr)
            sys.exit(1 if high_count > 0 else 0)
        else:
            if args.verbose:
                print(f"  No baseline snapshot found for {baseline_ref}. "
                      f"Falling back to normal exit logic.", file=sys.stderr)

    # Exit code: 1 if high-severity findings or debt_budget exceeded
    high_count = sum(1 for f in result.shown if f.get("severity", "").lower() == "high")

    # Step 7.5: Autonomous actions + summary
    if result.shown:
        # Count definite bugs (high severity + verified as definite with confidence >= 0.85)
        definite_bugs = []
        for f in result.shown:
            if f.get("severity", "").lower() == "high":
                tax = f.get("taxonomies", {})
                certainty = tax.get("certainty", "")
                confidence = f.get("_verify_confidence", 0)
                if certainty == "definite" and confidence >= 0.85:
                    definite_bugs.append(f)
        definite_count = len(definite_bugs)

        # Autonomous action 1: Auto-fix definite bugs (if not already done)
        if definite_count > 0 and not getattr(args, 'autofix', False):
            # Auto-generate and apply fixes for definite bugs
            try:
                from fixgen import generate_fixes, apply_fixes_locally
                print(f"\n🔧 確定バグ {definite_count}件に対して自動修正を生成中...", file=sys.stderr)
                fixes = generate_fixes(
                    definite_bugs, context,
                    model=args.model, backend=args.backend,
                    verbose=args.verbose,
                )
                if fixes:
                    applied = apply_fixes_locally(fixes, repo_path, verbose=args.verbose)
                    if applied:
                        print(f"✅ {len(applied)}件の修正を自動適用しました:", file=sys.stderr)
                        for fix in applied:
                            explanation = fix.get("explanation", "")
                            print(f"   {fix.get('file', '?')}:{fix.get('line', '?')} — {explanation}",
                                  file=sys.stderr)
                    else:
                        if args.verbose:
                            print("   ⚠ 修正コードは生成されましたが、適用できませんでした（コード変更の可能性）", file=sys.stderr)
                else:
                    if args.verbose:
                        print("   ⚠ 自動修正の生成に失敗しました", file=sys.stderr)
            except Exception as e:
                if args.verbose:
                    print(f"   ⚠ 自動修正の実行中にエラー: {e}", file=sys.stderr)

        # Autonomous action 2: Auto-suppress low severity findings (background)
        auto_suppressed_count = 0
        if len(result.filtered) > 0:
            # Auto-suppress low severity findings that are unlikely to be fixed
            try:
                from suppress import finding_hash, code_hash
                existing = load_suppressions(repo_path)
                new_suppressions = []
                for f in result.filtered:
                    # Only auto-suppress if certainty is uncertain and severity is low
                    tax = f.get("taxonomies", {})
                    certainty = tax.get("certainty", "uncertain")
                    sev = f.get("severity", "low").lower()
                    if sev == "low" and certainty == "uncertain":
                        fhash = finding_hash(f)
                        # Check if not already suppressed
                        if not any(e.finding_hash == fhash for e in existing + new_suppressions):
                            entry = SuppressEntry(
                                id=fhash,
                                finding_hash=fhash,
                                code_hash=code_hash(f, repo_path),
                                reason="自動suppress: 低重要度かつ不確実なfinding",
                                created_at=datetime.now().isoformat(),
                            )
                            new_suppressions.append(entry)
                            auto_suppressed_count += 1
                if new_suppressions:
                    save_suppressions(repo_path, existing + new_suppressions)
                    if args.verbose:
                        print(f"   📝 {auto_suppressed_count}件の低重要度 findings を自動suppressしました", file=sys.stderr)
            except Exception:
                pass  # Non-critical — don't fail scan for auto-suppress errors

        # Print final summary report
        print("\n" + "═" * 70, file=sys.stderr)
        print("📊 スキャン完了レポート", file=sys.stderr)
        print("═" * 70, file=sys.stderr)

        if definite_count > 0:
            print(f"✅ 確定バグ: {definite_count}件（2段階検証済み）", file=sys.stderr)
            if not getattr(args, 'autofix', False):
                print("   → 自動修正を試行しました（上記を確認してください）", file=sys.stderr)
        if high_count > definite_count:
            print(f"⚠ 高重要度: {high_count - definite_count}件", file=sys.stderr)
        if len(result.shown) > high_count:
            print(f"📋 その他: {len(result.shown) - high_count}件", file=sys.stderr)

        if len(result.filtered) > 0:
            print(f"📝 低重要度除外: {len(result.filtered)}件", end="", file=sys.stderr)
            if auto_suppressed_count > 0:
                print(f"（うち{auto_suppressed_count}件を自動suppress）", end="", file=sys.stderr)
            print("", file=sys.stderr)

        if verification_meta:
            print(f"🔍 検証結果: {verification_meta.get('confirmed', 0)}件確認、{verification_meta.get('rejected', 0)}件却下", file=sys.stderr)

        print("═" * 70 + "\n", file=sys.stderr)

    # debt_budget gate (CI integration)
    debt_budget = policy.get("debt_budget") if policy else None
    if debt_budget is not None:
        from findings import finding_debt_score
        from scoring import load_scoring_config
        _scoring_overrides = policy.get("scoring_weights") if policy else None
        scoring_cfg = load_scoring_config(repo_path, profile_overrides=_scoring_overrides)
        active_debt = sum(finding_debt_score(f, scoring_cfg) for f in result.shown)
        if active_debt > debt_budget:
            print(f"\n⚠ Debt budget exceeded: {active_debt:.1f} > {debt_budget} (budget)",
                  file=sys.stderr)
            sys.exit(1)
        elif args.verbose:
            print(f"  Debt budget OK: {active_debt:.1f} <= {debt_budget}", file=sys.stderr)

    sys.exit(1 if high_count > 0 else 0)


def _recover_existing_findings(repo_path: str, existing_json: Path) -> int:
    """Recover findings from existing_findings.json into JSONL.

    Called when init was interrupted: scan_existing wrote results to
    existing_findings.json but the JSONL conversion loop didn't complete.
    """
    import json as _json
    try:
        data = _json.loads(existing_json.read_text(encoding="utf-8"))
        results = data.get("results", [])
        repo_name = Path(repo_path).name

        from findings import Finding, generate_id, add_finding
        saved = 0
        for cluster in results:
            for f in cluster.get("findings", []):
                loc = f.get("location", {})
                file_a = loc.get("file_a", "") if isinstance(loc, dict) else ""
                file_b = loc.get("file_b", "") if isinstance(loc, dict) else ""
                pattern = f.get("pattern", "")
                title = f.get("contradiction", f.get("title", ""))[:120]
                fid = generate_id(repo_name, file_a, title,
                                  file_b=file_b, pattern=pattern)
                try:
                    from git_enrichment import enrich_finding
                    enrich_finding(f, repo_path)
                except Exception:
                    pass
                finding = Finding(
                    id=fid,
                    repo=repo_name,
                    file=file_a,
                    severity=f.get("severity", "medium"),
                    pattern=pattern,
                    title=title,
                    description=f.get("impact", f.get("user_impact", "")),
                    category=f.get("category", "contradiction"),
                    found_by="delta-init (recovered)",
                    churn_6m=f.get("churn_6m", 0),
                    fan_out=f.get("fan_out", 0),
                    total_lines=f.get("total_lines", 0),
                )
                try:
                    add_finding(repo_path, finding)
                    saved += 1
                except ValueError:
                    pass
        return saved
    except Exception:
        return 0
