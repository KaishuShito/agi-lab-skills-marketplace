# delta-lint Architecture

> **Status: Active Development (2026-03)**
> このドキュメントは開発フェーズ中に頻繁に更新される。安定後は更新頻度が下がる。
> 設計判断の経緯は `docs/decisions/` の ADR を参照。

---

## What

delta-lint は **コードモジュール間の構造矛盾** を LLM で検出するツール。
「ファイル A の前提とファイル B の実装が矛盾している」箇所を見つける。
スタイル違反や一般的なバグを探すものではない。

**核心の洞察**: バグの77%は同一著者が書いている。開発者は合理的にスコープを絞るが、
スコープ外で暗黙の契約が破れている場所をチェックする手段がない。delta-lint がそれを補う。

## Why This Design

- **LLM は非決定的** → 毎回の検出結果はブレる。だから findings は JSONL append-only で蓄積し、
  一度見つけたものは消えない（ストック型）
- **コスト $0 が最優先** → `claude -p`（サブスク CLI）をデフォルト、API は fallback
- **2段階パイプライン** → Phase 1: 高 recall（見逃さない）、Phase 2: FP 除去（精度向上）
- **1-hop 依存のみ** → コンテキスト爆発を防ぐ。足りなければ semantic 拡張で補う

---

## Module Map

```
scripts/
├── cli.py                  # CLI エントリポイント（scan / suppress / findings / view）
├── detector.py             # Phase 1: LLM 検出（高 recall）
├── verifier.py             # Phase 2: LLM 検証（FP 除去）
├── retrieval.py            # コンテキスト構築（git diff → import 解析 → 1-hop 依存）
├── semantic.py             # セマンティック拡張（暗黙仮定抽出 → grep 検索）
├── output.py               # フィルタリング（severity / suppress 適用）
├── findings.py             # JSONL 永続化（append-only イベントログ）
├── suppress.py             # サプレス管理（suppress.yml）
├── scoring.py              # スコアリング重み設定（3層マージ）
├── info_theory.py          # 情報理論スコア（surprise × entropy）
├── git_enrichment.py       # git メタデータ（churn / fan-out）
├── fixgen.py               # 修正コード生成
├── debt_loop.py            # 負債解消ループ（優先度付け → fix 生成）
├── stress_test.py          # 地雷マップ（仮想改修 × N → heatmap）
├── sibling.py              # 兄弟マップ（A↔B 暗黙契約追跡）
├── cache.py                # スキャンキャッシュ（context_hash ベース）
├── contract_graph.py       # 構造契約グラフ（WordPress フック等）
├── surface_extractor.py    # サーフェス抽出（hook/action パターン）
├── deep_verifier.py        # 深層検証（contract_graph の候補検証）
├── persona_translator.py   # ペルソナ翻訳（engineer/pm/qa 向け変換）
├── aggregate.py            # stress_test 結果集約
├── visualize.py            # HTML 地雷マップ生成
├── intro_animation.py      # TUI アニメーション（デモ用）
├── prompts/
│   ├── detect.md           # 検出プロンプト（6+4 パターン定義）
│   ├── detect_existing.md  # 既存バグスキャン用プロンプト
│   ├── verify.md           # Phase 2 検証プロンプト
│   ├── structure_analysis.md    # 構造分析プロンプト
│   ├── generate_modifications.md      # 仮想改修生成
│   └── generate_focused_modifications.md  # ホットスポット改修生成
├── profiles/
│   ├── _reference.yml      # 全フィールドリファレンス（スキャン対象外）
│   ├── deep.yml            # 全パターン・全重大度・semantic ON
│   ├── light.yml           # high のみ・高速 CI ゲート用
│   └── security.yml        # セキュリティ特化
└── templates/
    ├── dashboard.html           # 地雷マップ HTML テンプレート
    └── findings_dashboard.html  # findings ダッシュボード HTML テンプレート
```

### Entry Points

| 呼び出し元 | ファイル | 用途 |
|-----------|---------|------|
| CLI | `scripts/cli.py` | 開発者がローカルで使う |
| GitHub Action | `action/entrypoint.py` | PR レビュー自動化 |
| Claude Code Skill | `cli.py` 経由 | `/delta-scan` 等のスキルから |

### Stability Guide

| モジュール | 安定度 | 変更する前に読むべきもの |
|-----------|--------|----------------------|
| `retrieval.py` | **安定** | ADR-001 |
| `detector.py` / `verifier.py` | **安定** | prompts/*.md のフォーマット仕様 |
| `findings.py` | **安定** | JSONL フォーマットは後方互換必須 |
| `scoring.py` | **安定** | ADR-002 |
| `output.py` / `suppress.py` | **安定** | — |
| `cli.py` | **頻繁に変更** | 設定マージの優先順位チェーン |
| `fixgen.py` | **開発中** | ADR-003 |
| `debt_loop.py` | **開発中** | — |
| `info_theory.py` | **開発中** | — |
| `stress_test.py` | **安定（拡張予定）** | — |
| `contract_graph.py` | **実験的** | WordPress 以外は未対応 |
| `prompts/*.md` | **頻繁に変更** | 検出精度に直結。変更後は必ずテスト |

---

## Data Flow

```
git diff (変更ファイル特定)
    │
    ▼
retrieval.py ─── build_context()
    │  import 解析 → 1-hop 依存取得
    │  confidence tier: direct(0.95) / re-export(0.85) / type(0.50)
    │  [optional: --docs] ドキュメント契約面を追加（README, ADR 等）
    │
    ▼  [optional: --semantic]
semantic.py ─── expand_context()
    │  LLM で暗黙仮定を抽出 → git grep で関連ファイル発見
    │
    ▼
detector.py ─── detect()
    │  prompts/detect.md を system prompt として LLM に送信
    │  → raw findings JSON（高 recall、FP あり）
    │
    ▼  [optional: --no-verify でスキップ]
verifier.py ─── verify()
    │  prompts/verify.md で各 finding を再検証
    │  → confidence < threshold のものを除外
    │
    ▼
output.py ─── filter_findings()
    │  severity フィルタ + suppress.yml マッチ
    │  → shown / filtered / suppressed / expired に分類
    │
    ▼
findings.py ─── append_finding() / generate_dashboard()
    │  JSONL に追記（同 ID = イベント履歴、最新行が現状態）
    │  HTML ダッシュボード生成
    │
    ▼  [optional: --autofix]
fixgen.py ─── generate_fixes()
       LLM で最小修正コード生成 → ローカル適用
```

---

## Configuration System

### Priority Chain

```
CLI flags  >  profile (.delta-lint/profiles/<name>.yml)  >  config.json (.delta-lint/config.json)  >  defaults
```

明示的に指定された上位が常に勝つ。

### Profile Structure

```yaml
name: my-profile
description: "説明"

config:       # ← engine パラメータ（argparse のフラグに対応）
  severity: medium
  model: claude-sonnet-4-20250514
  max_context_chars: 80000    # retrieval 定数もここ
  max_file_chars: 30000

policy:       # ← 検出ロジック制御（ランタイム挙動）
  prompt_append: "追加指示"
  disabled_patterns: ["⑦", "⑩"]
  scoring_weights: { ... }
  dashboard_template: ".delta-lint/templates/custom.html"
```

**config vs policy の判断基準**:
- config = 「何を使うか」（モデル、閾値、容量制限）
- policy = 「どう動くか」（プロンプト、パターン、重み、テンプレート）

→ 詳細は ADR-001 参照。

### Scoring: 3-Layer Merge

```
defaults (scoring.py)  ←  config.json  ←  profile policy
```

指定されたキーだけ上書き。未指定はデフォルト値を使う。

---

## Storage Layout (.delta-lint/)

```
.delta-lint/
├── config.json              # リポ固有のデフォルト設定
├── suppress.yml             # サプレスされた findings
├── sibling_map.yml          # A↔B 暗黙契約マップ（auto-learn）
├── scan_history.jsonl       # スキャン時系列ログ
├── cache/                   # context_hash ベースのスキャンキャッシュ
├── findings/
│   ├── {repo-name}.jsonl    # findings イベントログ（append-only）
│   ├── dashboard.html       # 生成済みダッシュボード
│   └── _index.md            # findings サマリー
├── profiles/                # リポ固有のカスタムプロファイル
│   └── my-team.yml
└── templates/               # リポ固有のカスタムテンプレート
    └── findings_dashboard.html
```

---

## Key Design Decisions

設計判断の詳細は `docs/decisions/` にある。ここでは要約のみ。

| ADR | 判断 | 一言 |
|-----|------|------|
| [001](docs/decisions/001-retrieval-config-not-in-argparse.md) | retrieval 定数を argparse に入れない | CLI namespace 汚染防止 |
| [002](docs/decisions/002-scoring-three-layer-merge.md) | スコアリングは 3層マージ | チーム別カスタマイズ |
| [003](docs/decisions/003-cli-backend-zero-cost.md) | CLI バックエンド（$0）をデフォルト | コスト意識 |
| [004](docs/decisions/004-findings-append-only-jsonl.md) | findings は append-only JSONL | LLM 非決定性への対策 |
| [005](docs/decisions/005-dashboard-template-resolution.md) | テンプレート 3 段解決 | カスタマイズ性 vs 簡便性 |
| [006](docs/decisions/006-document-contract-surfaces.md) | ドキュメントを契約面として扱う | コード × ドキュメント矛盾検出 |

---

## Detection Patterns

### Contradiction Patterns (①-⑥) — category: "contradiction"
| # | Name | 必要な箇所数 |
|---|------|-------------|
| ① | Asymmetric Defaults | 2箇所 |
| ② | Semantic Mismatch | 2箇所 |
| ③ | External Spec Divergence | 2箇所 |
| ④ | Guard Non-Propagation | 2箇所 |
| ⑤ | Paired-Setting Override | 2箇所 |
| ⑥ | Lifecycle Ordering | 2箇所 |

### Technical Debt Patterns (⑦-⑩) — category: "debt"
| # | Name | 必要な箇所数 |
|---|------|-------------|
| ⑦ | Dead Code / Unreachable Path | 1箇所可 |
| ⑧ | Duplication Drift | 2箇所 |
| ⑨ | Interface Mismatch | 2箇所 |
| ⑩ | Missing Abstraction | 1箇所可（3+出現） |

詳細は `scripts/prompts/detect.md` を参照。

---

## Testing

現時点では自動テストスイートはない（開発フェーズ）。検証は以下で行う：

```bash
# パイプライン動作確認（LLM 呼び出しなし）
python scripts/cli.py scan --repo /path/to/repo --dry-run

# 単一ファイルスキャン
python scripts/cli.py scan --repo /path/to/repo --files path/to/file.py

# プロファイル適用確認
python scripts/cli.py scan --repo /path/to/repo -p deep --dry-run

# findings ダッシュボード生成
python scripts/cli.py findings dashboard --repo /path/to/repo
```

---

## Glossary

| 用語 | 意味 |
|------|------|
| **finding** | 検出された構造矛盾または技術的負債の1件 |
| **contradiction** | 2つのモジュール間で暗黙の契約が破れている状態 |
| **sibling** | 暗黙の契約で結ばれた2ファイル（A が変わったら B も確認すべき） |
| **suppress** | finding を意図的に無視する（理由付き、期限付き） |
| **churn** | 過去6ヶ月のコミット数（変更頻度の指標） |
| **fan-out** | あるファイルを参照している他ファイル数（影響範囲の指標） |
| **debt_score** | 技術的負債の定量値（severity × pattern × status） |
| **roi_score** | 修正の費用対効果（churn × fan_out / fix_cost） |
| **info_score** | 情報理論的な驚き度（surprise × entropy） |
| **landmine map** | 仮想改修で爆発しやすい箇所の heatmap |
