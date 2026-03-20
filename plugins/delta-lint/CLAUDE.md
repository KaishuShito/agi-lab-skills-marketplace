# delta-lint 開発ガイド

## シンボリックリンク構造（最重要）

マスター（ファイル編集はここ）:
```
agi-lab-skills-marketplace/plugins/delta-lint/scripts/
```

実行時パス（シンボリックリンク。**直接編集するな**）:
```
~/.claude/skills/delta-lint/scripts/ → マスターへのリンク
~/.claude/skills/delta-scan/scripts/ → マスターへのリンク
```

ワークフロー内のコマンド（`cd ~/.claude/skills/delta-lint/scripts && python ...`）は実行時パスを使う。
コード編集はマスターで行い、symlink 経由で実行時に反映される。

## アーキテクチャ制約

### データ（append-only ログ）
- `findings.jsonl` — append-only イベントログ。同一IDの後発エントリが最新状態。直接編集禁止。API: `add_finding()` / `update_finding()`
- `scan_history.jsonl` — append-only。`finding_ids` と `patterns_found` は Chao1 カバレッジ推定に使用
- `generate_id()` — ファイルペア+パターンからの構造ベースハッシュ。LLMの表現揺れに依存しない。**ロジック変更禁止**（IDが変わると重複発生）

### スコアリング
- `scoring.py` が唯一の重み定義元。findings.py は scoring.py から import する。独自の重みを定義するな
- `info_theory.py` — 情報理論ベースのスコアリング（surprise, Chao1, entropy）。scoring.py と並行して存在

### テンプレート
- `templates/findings_dashboard.html` — Python の `string.Template` (`$variable`)。Jinja2 ではない。`${}` は使えるが `{% %}` は使えない

### taxonomies
- `dict` 型で自由にキー追加可能。値は `str | list[str]`
- 旧 `category` フィールドは後方互換用。新規コードは `taxonomies` を使え
- `_migrate_taxonomies()` が旧→新の変換を担当

### LLM 呼び出し
- `claude -p` (CLI) を使え。Anthropic API 直叩き禁止（コスト理由）
- `detector.py` の `_cli_available()` パターンを参照: CLI → API フォールバック

## モジュール依存関係

```
cli.py              ─── メイン CLI エントリポイント（1120行）。argparse + view/suppress/config コマンド
├── cli_utils.py        ─── 共通ユーティリティ（750行）。環境チェック、config/profile 読込、ベースライン
├── cmd_scan.py         ─── scan コマンド群（1560行）。cmd_scan, cmd_scan_deep, cmd_scan_full, cmd_watch
├── cmd_init.py         ─── init コマンド（507行）。リポジトリ初期化
├── detector.py         ─── LLM スキャン（通常）
├── retrieval.py        ─── ファイル取得 + import 依存解析
├── findings.py         ─── JSONL 管理 + ダッシュボード生成
│   ├── scoring.py          ─── スコアリング重み（ROI, debt_score）
│   └── info_theory.py      ─── 情報理論スコアリング（surprise, Chao1）
├── surface_extractor.py ─── Deep scan Phase 0（正規表現抽出）
├── contract_graph.py    ─── 契約グラフ検出（WordPress 等フック系向け、レガシー）
├── deep_verifier.py     ─── Deep scan Phase 2（LLM 検証、contract_graph 用）
│   ※ --depth deep は通常スキャンと同じ LLM パスで max_hops=3 の依存解決を行う
├── git_enrichment.py    ─── Git churn/fan_out 計算（スキャン時に finding へ埋め込み）
├── output.py            ─── 表示フォーマット
├── suppress.py          ─── 抑制管理
├── fixgen.py            ─── Autofix 生成
├── debt_loop.py         ─── 自動負債解消ループ（finding → branch → fix → PR）
└── stress_test.py       ─── ストレステスト + 地雷マップ
```

## 実行方法

```bash
cd scripts/
python cli.py scan --repo /path/to/repo         # 通常スキャン
python cli.py scan --repo /path/to/repo --depth deep   # 深層スキャン（依存チェーンを辿る）
python cli.py findings list --repo /path/to/repo # Findings 一覧
python cli.py findings dashboard --repo /path    # ダッシュボード生成
```

## PR/コミットのルール

- 「Claude Code」「Generated with」等のブランディングを入れるな
- Co-Authored-By 行も入れるな
