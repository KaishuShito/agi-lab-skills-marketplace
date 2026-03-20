"""cmd_init – extracted from cli.py for modularity."""

import sys
from pathlib import Path

from cli_utils import _open_dashboard


def cmd_init(args):
    """Initialize delta-lint for a repository (lightweight)."""
    repo_path = str(Path(args.repo).resolve())

    # Intro animation (skip if --quiet or non-TTY)
    if not getattr(args, 'quiet', False) and sys.stderr.isatty():
        try:
            from intro_animation import run_animation
            run_animation()
        except Exception:
            pass

    print("── δ-lint ── 初期化開始", file=sys.stderr)

    # Step 0.5: Git history analysis → initial sibling map
    print("  Git 履歴を解析中...", file=sys.stderr)
    git_siblings_count = 0
    try:
        from sibling import (
            generate_siblings_from_git_history,
            load_sibling_map,
            save_sibling_map,
            get_git_churn,
        )
        # Generate siblings from co-change history
        new_siblings = generate_siblings_from_git_history(
            repo_path, months=6, min_co_changes=3, verbose=args.verbose,
        )
        if new_siblings:
            existing = load_sibling_map(repo_path)
            all_entries = existing + new_siblings
            save_sibling_map(repo_path, all_entries)
            git_siblings_count = len(new_siblings)

        # Get churn data for display
        churn_data = get_git_churn(repo_path, months=6)
        if churn_data:
            print(f"  📊 よく改修されるファイル TOP 5:", file=sys.stderr)
            for item in churn_data[:5]:
                print(f"    {item['path']} ({item['changes']}回変更)", file=sys.stderr)

        if git_siblings_count:
            print(f"  🔗 co-change ペア: {git_siblings_count} 件を sibling_map に追加", file=sys.stderr)
    except Exception as e:
        if args.verbose:
            print(f"  [warn] Git history analysis failed: {e}", file=sys.stderr)

    # Step 1: Structure analysis (LLM)
    print("  構造解析を実行中（~30秒）...", file=sys.stderr)

    from stress_test import init_lightweight
    structure = init_lightweight(repo_path, verbose=args.verbose)

    modules = structure.get("modules", [])
    hotspots = structure.get("hotspots", [])

    print(f"\n  {len(modules)} モジュール, {len(hotspots)} ホットスポット検出", file=sys.stderr)
    if hotspots:
        print("  🔥 変更リスクが高いファイル:", file=sys.stderr)
        for h in hotspots[:5]:
            path = h.get("path", h.get("file", ""))
            reason = h.get("reason", "")
            print(f"    {path} — {reason}", file=sys.stderr)

    # Show dev_patterns (from git history analysis)
    dev_patterns = structure.get("dev_patterns", [])
    if dev_patterns:
        _PATTERN_ICONS = {
            "bug-prone": "🐛", "expanding": "📈", "refactoring": "🔧",
            "stable": "✅", "single-owner": "👤",
        }
        print(f"\n  📊 開発パターン分析 ({len(dev_patterns)} エリア):", file=sys.stderr)
        for dp in dev_patterns[:8]:
            icon = _PATTERN_ICONS.get(dp.get("pattern", ""), "❓")
            area = dp.get("area", "?")
            pattern = dp.get("pattern", "?")
            risk = dp.get("risk", "")
            print(f"    {icon} {area} [{pattern}]", file=sys.stderr)
            if risk:
                print(f"      → {risk[:120]}", file=sys.stderr)

    # Show constraints info
    constraints_path = Path(repo_path) / ".delta-lint" / "constraints.yml"
    if constraints_path.exists():
        print(f"\n  📋 constraints.yml 生成済み: {constraints_path}", file=sys.stderr)
        print("    ベテランの知識を追記すると scan 精度が上がります", file=sys.stderr)

    # Show sibling map summary
    try:
        from sibling import load_sibling_map as _load_sib
        all_sibs = _load_sib(repo_path)
        if all_sibs:
            git_count = sum(1 for s in all_sibs if s.source == "git-history")
            finding_count = sum(1 for s in all_sibs if s.source == "finding")
            print(f"\n  🔗 sibling_map: {len(all_sibs)} ペア（git履歴: {git_count}, finding: {finding_count}）", file=sys.stderr)
    except Exception:
        pass

    # Show progressive scan coverage
    try:
        from stress_test import load_coverage
        coverage = load_coverage(repo_path)
        n_covered = len(coverage.get("scanned_files", {}))
        total_scans = coverage.get("total_scans", 0)
        if total_scans > 0:
            from stress_test import _list_source_files
            n_total = len(_list_source_files(repo_path))
            pct = round(n_covered / max(n_total, 1) * 100)
            print(f"\n  📈 スキャンカバレッジ: {n_covered}/{n_total} ファイル ({pct}%) — {total_scans}回実行", file=sys.stderr)
            if pct < 100:
                print(f"    → 再実行するとカバレッジが拡大します（未分析エリアを優先）", file=sys.stderr)
    except Exception:
        pass

    # Step 2: Parallel — scan_existing (findings) + stress test (landmine map)
    if hotspots:
        import threading

        # --- Stress test thread (landmine map) ---
        stress_result = {"error": None, "added": 0}

        def _run_stress_test_bg():
            try:
                from stress_test import run_stress_test
                # Init mode: use shorter time (5 minutes for faster init)
                run_stress_test(
                    repo_path,
                    backend="cli",
                    verbose=args.verbose,
                    parallel=5,
                    lang="en",
                    structure=structure,
                    skip_existing=True,
                    max_wall_time=300,  # 5 minutes (for faster init)
                    n_modifications=5,  # init は軽量に
                )
                from findings import ingest_stress_test_debt
                added = ingest_stress_test_debt(repo_path)
                stress_result["added"] = len(added) if added else 0
            except Exception as e:
                stress_result["error"] = str(e)

        print("\n  🔨 ストレステスト（地雷マップ）をバックグラウンドで開始...", file=sys.stderr)
        stress_thread = threading.Thread(target=_run_stress_test_bg, daemon=True)
        stress_thread.start()

        # --- scan_existing (findings → JSONL → dashboard) on main thread ---
        print("  🔍 既存コードの矛盾をスキャン中...", file=sys.stderr)
        try:
            from stress_test import scan_existing
            from findings import Finding, generate_id, add_finding, generate_dashboard, list_findings
            import webbrowser

            repo_name = Path(repo_path).name
            n_saved = 0
            n_findings = 0
            dashboard_opened = False
            all_fids = []
            all_patterns = []
            EARLY_DASHBOARD_THRESHOLD = 3  # Open dashboard after first 3 findings

            for result, completed, total in scan_existing(
                structure, repo_path,
                backend="cli", verbose=args.verbose, parallel=3,
                stream=True,
            ):
                # Save findings from this cluster
                cluster_new = 0
                for f in result.get("findings", []):
                    n_findings += 1
                    loc = f.get("location", {})
                    file_a = loc.get("file_a", "")
                    file_b = loc.get("file_b", "")
                    pattern = f.get("pattern", "")
                    title = f.get("contradiction", f.get("title", ""))[:120]
                    fid = generate_id(repo_name, file_a, title,
                                      file_b=file_b, pattern=pattern)
                    # Enrich with git data (churn, fan_out, total_lines)
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
                        found_by="delta-init",
                        churn_6m=f.get("churn_6m", 0),
                        fan_out=f.get("fan_out", 0),
                        total_lines=f.get("total_lines", 0),
                    )
                    all_fids.append(fid)
                    if pattern:
                        all_patterns.append(pattern)
                    try:
                        add_finding(repo_path, finding)
                        n_saved += 1
                        cluster_new += 1
                    except ValueError:
                        pass  # duplicate — skip

                is_complete = (completed == total)
                progress = {"completed": completed, "total": total, "is_complete": is_complete}

                # Build treemap JSON if results.json exists (from stress test running in parallel)
                _treemap = None
                _results_json = Path(repo_path) / ".delta-lint" / "stress-test" / "results.json"
                if _results_json.exists():
                    try:
                        from visualize import build_treemap_json
                        _treemap = build_treemap_json(str(_results_json))
                    except Exception:
                        pass

                # Regenerate dashboard with progress info
                _dash_tpl = getattr(args, '_dashboard_template', "")
                dash_path = generate_dashboard(repo_path, scan_progress=progress, treemap_json=_treemap, dashboard_template=_dash_tpl)

                # Early dashboard open: after first few findings, open live dashboard
                if (not dashboard_opened and n_saved >= EARLY_DASHBOARD_THRESHOLD
                        and not getattr(args, 'no_open', False)):
                    if _open_dashboard(str(dash_path), live=True):
                        dashboard_opened = True
                        print(f"\n  📊 ライブダッシュボードを開きました（{n_saved}件検出済み）。"
                              f" リロードで最新表示。スキャン継続中...", file=sys.stderr)
                elif not dashboard_opened and n_saved > 0 and not getattr(args, 'no_open', False):
                    # Fallback: open on first finding if threshold not reached yet
                    if _open_dashboard(str(dash_path), live=True):
                        dashboard_opened = True
                        print(f"\n  📊 ダッシュボードを開きました（{n_saved}件検出済み）。"
                              f" スキャンはバックグラウンドで継続中...", file=sys.stderr)

                print(f"  [{completed}/{total}] {cluster_new} 件検出 (累計 {n_findings} 件)", file=sys.stderr)

            print(f"  🔍 {n_findings} 件検出、{n_saved} 件を findings に記録", file=sys.stderr)

            # Check for definite bugs (high severity + verified as definite)
            definite_bugs = 0
            try:
                from findings import list_findings
                all_recorded = list_findings(repo_path)
                for f in all_recorded:
                    if f.get("severity", "").lower() == "high":
                        tax = f.get("taxonomies", {})
                        certainty = tax.get("certainty", "")
                        if certainty == "definite":
                            definite_bugs += 1
            except Exception:
                pass

            # Guarantee mode: if no definite bugs found, expand scan scope
            if definite_bugs == 0:
                print(f"\n  ⚠ 確定バグが見つかりませんでした。スキャン範囲を拡大します...", file=sys.stderr)

                # Fallback strategy: try different scan approaches
                fallback_attempts = [
                    ("wide", "default", "default", "全ファイルスキャン"),
                    ("smart", "deep", "default", "深い依存関係までスキャン"),
                    ("smart", "default", "security", "セキュリティ観点でスキャン"),
                ]

                for scope, depth, lens, desc in fallback_attempts:
                    print(f"  🔍 {desc}を試行中...", file=sys.stderr)
                    try:
                        # Create a minimal args object for cmd_scan
                        class ScanArgs:
                            def __init__(self):
                                self.repo = repo_path
                                self.scope = scope
                                self._depth = depth
                                self._lens = lens
                                self.severity = "high"
                                self.lang = "ja"
                                self.verbose = args.verbose
                                self.model = getattr(args, 'model', 'claude-sonnet-4-20250514')
                                self.backend = getattr(args, 'backend', 'cli')
                                self.no_cache = False
                                self.no_verify = False
                                self.autofix = False
                                self.no_open = True  # Don't open dashboard during fallback
                                self.files = None
                                self.since = None
                                self.diff_target = None
                                self.baseline = None
                                self.baseline_save = False
                                self.watch_interval = 3.0
                                self.semantic = False
                                self.docs = None
                                self.parallel = 3
                                self.deep_workers = 4
                                self.persona = None
                                self.output_format = "markdown"
                                self.log_dir = None
                                self.no_learn = False

                        scan_args = ScanArgs()
                        # Run a quick scan with limited context
                        from detector import detect
                        from context_builder import build_context
                        from verifier import verify_findings

                        # Get target files based on scope
                        if scope == "wide":
                            from retrieval import get_all_source_files
                            target_files = get_all_source_files(repo_path)[:50]  # Limit to 50 files
                        else:
                            from retrieval import get_priority_batches
                            batches = get_priority_batches(repo_path)
                            target_files = []
                            for batch in batches[:3]:  # First 3 batches only
                                target_files.extend(batch)

                        if not target_files:
                            continue

                        # Build context
                        context = build_context(
                            repo_path, target_files,
                            depth=depth, lens=lens,
                            verbose=args.verbose,
                        )

                        # Detect
                        findings = detect(
                            context,
                            model=scan_args.model,
                            backend=scan_args.backend,
                            verbose=args.verbose,
                        )

                        # Verify
                        if findings and not scan_args.no_verify:
                            findings, _, _ = verify_findings(
                                findings, context,
                                model=scan_args.model,
                                backend=scan_args.backend,
                                verbose=args.verbose,
                            )

                        # Check for definite bugs
                        found_definite = False
                        for f in findings:
                            if f.get("severity", "").lower() == "high":
                                tax = f.get("taxonomies", {})
                                certainty = tax.get("certainty", "")
                                confidence = f.get("_verify_confidence", 0)
                                if certainty == "definite" and confidence >= 0.85:
                                    found_definite = True
                                    # Save this finding
                                    loc = f.get("location", {})
                                    file_a = loc.get("file_a", "")
                                    file_b = loc.get("file_b", "")
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
                                        found_by="delta-init-fallback",
                                        taxonomies=f.get("taxonomies"),
                                        churn_6m=f.get("churn_6m", 0),
                                        fan_out=f.get("fan_out", 0),
                                        total_lines=f.get("total_lines", 0),
                                    )
                                    try:
                                        add_finding(repo_path, finding)
                                        n_saved += 1
                                        n_findings += 1
                                        all_fids.append(fid)
                                        if pattern:
                                            all_patterns.append(pattern)
                                        print(f"  ✅ 確定バグを検出: {title[:60]}...", file=sys.stderr)
                                    except ValueError:
                                        pass  # duplicate
                                    break

                        if found_definite:
                            break
                    except Exception as e:
                        if args.verbose:
                            print(f"  [warn] フォールバックスキャン失敗: {e}", file=sys.stderr)
                        continue

                # Final check
                try:
                    all_recorded = list_findings(repo_path)
                    definite_bugs = 0
                    for f in all_recorded:
                        if f.get("severity", "").lower() == "high":
                            tax = f.get("taxonomies", {})
                            certainty = tax.get("certainty", "")
                            if certainty == "definite":
                                definite_bugs += 1
                except Exception:
                    pass

                if definite_bugs == 0:
                    print(f"\n  ⚠ 警告: 確定バグを1件も検出できませんでした。", file=sys.stderr)
                    print(f"     コードベースが非常に健全な可能性がありますが、", file=sys.stderr)
                    print(f"     スキャン範囲や設定を確認してください。", file=sys.stderr)
                else:
                    print(f"\n  ✅ 確定バグ {definite_bugs}件を検出しました（拡大スキャンで発見）", file=sys.stderr)

            try:
                from findings import append_scan_history
                append_scan_history(
                    repo_path,
                    clusters=total,
                    findings_count=n_findings,
                    scan_type="existing",
                    finding_ids=all_fids,
                    patterns_found=all_patterns,
                    scope="smart",
                    depth="default",
                    lens="default",
                )
            except Exception:
                pass
        except Exception as e:
            if args.verbose:
                import traceback
                traceback.print_exc()
            print(f"  [warn] Existing scan failed: {e}", file=sys.stderr)

        # --- Wait for stress test to finish ---
        if stress_thread.is_alive():
            print("  ⏳ ストレステスト完了を待機中...", file=sys.stderr)
        stress_thread.join(timeout=7200)
        if stress_thread.is_alive():
            print("  [warn] ストレステストがタイムアウト(2h)しました。スキップします。", file=sys.stderr)

        if stress_result["error"]:
            print(f"  [warn] ストレステスト失敗: {stress_result['error']}", file=sys.stderr)
        else:
            added = stress_result["added"]
            print(f"  🗺️  地雷マップ生成完了", file=sys.stderr)
            if added:
                print(f"    ストレステスト結果から {added} 件の技術的負債を登録", file=sys.stderr)

            # Final dashboard regeneration with treemap data from completed stress test
            try:
                _treemap = None
                _results_json = Path(repo_path) / ".delta-lint" / "stress-test" / "results.json"
                if _results_json.exists():
                    from visualize import build_treemap_json
                    _treemap = build_treemap_json(str(_results_json))
                if _treemap:
                    _dash_tpl = getattr(args, '_dashboard_template', "")
                    generate_dashboard(repo_path, treemap_json=_treemap, dashboard_template=_dash_tpl)
            except Exception:
                pass

    print("\n── δ-lint ── 初期化完了 ✅", file=sys.stderr)

    # Auto-open dashboard after init
    if not getattr(args, 'no_open', False):
        try:
            from findings import generate_dashboard
            treemap_json = None
            results_path = Path(repo_path) / ".delta-lint" / "stress-test" / "results.json"
            if results_path.exists():
                try:
                    from visualize import build_treemap_json
                    treemap_json = build_treemap_json(str(results_path))
                except Exception:
                    pass
            _dash_tpl = getattr(args, '_dashboard_template', "")
            dash_path = generate_dashboard(repo_path, treemap_json=treemap_json, dashboard_template=_dash_tpl)
            if dash_path:
                if _open_dashboard(str(dash_path), force=True, live=True):
                    print("  📊 ライブダッシュボードを開きました", file=sys.stderr)
        except Exception as e:
            if args.verbose:
                print(f"  [warn] ダッシュボードを開けませんでした: {e}", file=sys.stderr)

    print("  次のステップ:", file=sys.stderr)
    print("    delta scan                    — 変更ファイルをスキャン", file=sys.stderr)
    print("    delta scan --scope wide       — 全ファイルスキャン（バッチ分割）", file=sys.stderr)
    print("    delta scan --lens stress      — ストレステスト（地雷マップ生成）", file=sys.stderr)
    print("    delta scan --lens security    — セキュリティ重点スキャン", file=sys.stderr)
    print("    delta init          — 再実行でカバレッジ拡大", file=sys.stderr)
