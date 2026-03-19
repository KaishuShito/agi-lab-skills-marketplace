# DeltaLint

**構造矛盾検出器** — コードモジュール間の暗黙の前提が破れている箇所を LLM で検出します。

スタイルやシンプルなバグではなく、**設計レベルの不整合**（あるモジュールの前提が別のモジュールの振る舞いと矛盾している箇所）を見つけます。

## セットアップ

### 必須

- **Python 3.11+**
- **git** — diff ベースのスキャンに使用

### 自動セットアップ（手動インストール不要）

DeltaLint は初回スキャン時に不足している依存を**自動で検出・インストール**します。
手動でのセットアップは不要です。何もインストールされていない状態からでも、DeltaLint が自分で環境を整えます。

| 依存 | 自動解決方法 | 用途 |
|------|-------------|------|
| **claude CLI** | `npm install -g @anthropic-ai/claude-code` | LLM バックエンド（$0） |
| **gh CLI** | `brew install gh` / `conda install gh` → 自動で `gh auth login --web` | Issue/PR 自動作成 |
| **anthropic SDK** | `pip install anthropic` | API バックエンド（フォールバック） |
| **PyYAML** | `pip install pyyaml` | 設定ファイル読み込み |

いずれかのインストールが失敗しても、代替手段に自動フォールバックして動作を継続します：

```
claude CLI がない → Anthropic API にフォールバック
gh CLI がない    → Issue/PR 作成をスキップ（スキャン結果は正常に返す）
API キーもない   → dry-run モードで動作
```

## 使い方

### Claude Code から（推奨）

```
> delta scan                              # 変更ファイルのデグレチェック
> delta scan --scope smart                # git 履歴ベースでファイル選択
> delta scan --depth graph                # 契約グラフ解析（深層スキャン）
> delta scan --lens security              # セキュリティ特化
> delta scan --scope all --lens stress    # 全ファイル × ストレステスト
> delta view                              # ダッシュボードをブラウザで表示
> delta init                              # リポジトリの初期化（構造分析）
```

### CLI から直接

```bash
cd plugins/delta-lint/scripts

# 変更ファイルをスキャン（デフォルト: diff × 1hop × 構造矛盾検査）
python cli.py scan --repo /path/to/repo

# 3軸モデルで細かく指定
python cli.py scan --scope smart --depth graph --lens security

# プロファイルを使う（プリセット設定を一括適用）
python cli.py scan -p deep             # 徹底スキャン
python cli.py scan -p light            # CI向け高速チェック
python cli.py scan -p security         # セキュリティ特化

# 特定ファイルをスキャン
python cli.py scan --files src/handler.ts src/router.ts

# 全重要度を表示 + 日本語出力
python cli.py scan --severity low --lang ja

# 意味検索を有効化（暗黙の仮定を抽出して関連ファイルを拡張）
python cli.py scan --semantic

# ドキュメントを仕様契約として検査（コード × ドキュメント矛盾検出）
python cli.py scan --docs README.md ARCHITECTURE.md

# ウォッチモード（ファイル変更を監視して自動再スキャン）
python cli.py scan --watch

# 検出 + 自動修正コード生成
python cli.py scan --autofix

# API バックエンドを使用
python cli.py scan --backend api
```

## 3軸スキャンモデル

スキャンは **広さ（scope）× 深さ（depth）× 質（lens）** の3軸で制御します。

| 軸 | 選択肢 | 説明 |
|----|--------|------|
| **scope**（広さ） | `diff`（デフォルト） | 変更ファイル + 1-hop 依存 |
| | `smart` | git 履歴ベースのファイル選択（diff 不要） |
| | `all` | 全ソースファイル |
| **depth**（深さ） | `1hop`（デフォルト） | import ベースの 1-hop 依存 |
| | `graph` | 契約グラフ解析（surface extraction → LLM 検証） |
| **lens**（質） | `default`（デフォルト） | 構造矛盾検査（10パターン） |
| | `stress` | 仮想改修ストレステスト（地雷マップ生成） |
| | `security` | セキュリティ特化（認証・権限・入力検証） |

全組み合わせ: 3 × 2 × 3 = **18通り**。ダッシュボードの「スキャン状況」パネルで実行済み/未実行が一覧できます。

## 検出パターン

### 構造矛盾（contradiction）

2つのモジュール間で暗黙の契約が破れている箇所。

| # | パターン | 例 |
|---|---------|---|
| ① | Asymmetric Defaults | 登録時は `null` を受け入れるが表示時は `undefined` を空文字に変換 |
| ② | Semantic Mismatch | `status: 0` がモジュール A では "pending"、B では "inactive" |
| ③ | External Spec Divergence | RFC 7230 に準拠すると書いてあるが実装が逸脱 |
| ④ | Guard Non-Propagation | create エンドポイントにはバリデーションがあるが update にはない |
| ⑤ | Paired-Setting Override | `timeout=30s` と `retries=5` の組み合わせが上流の制限を超える |
| ⑥ | Lifecycle Ordering | エラーリカバリパスでは認証ミドルウェアがルートハンドラの後に実行される |

### 技術的負債（structural）

放置すると保守コストが増大する、構造的に改善すべき箇所。

| # | パターン | 例 |
|---|---------|---|
| ⑦ | Dead Code / Unreachable Path | エラーリカバリハンドラが登録されているが対応するエラー型は投げられない |
| ⑧ | Duplication Drift | コピー元の関数にはバリデーション追加済み、コピー先は未更新 |
| ⑨ | Interface Mismatch | 定義は `save(data, options?)` だが呼び出し側は3引数で呼んでいる |
| ⑩ | Missing Abstraction | 同一の条件チェック＋処理が5つのコントローラに散在 |

## スコアリング

各 finding に3種類のスコアが付与されます。数値は直感的な桁感になるよう設計されています。

### 負債スコア（debt_score）— 0〜1000

```
debt_score = severity × pattern × status × 1000
```

| 例 | severity | pattern | status | スコア |
|---|----------|---------|--------|-------|
| high + ① + found | 1.0 | 1.0 | 1.0 | **1000** |
| medium + ④ + found | 0.6 | 1.0 | 1.0 | **600** |
| high + ⑧ + submitted | 1.0 | 0.6 | 0.8 | **480** |
| low + ⑦ + found | 0.3 | 0.3 | 1.0 | **90** |
| any + merged | — | — | 0.0 | **0** |

merged や wontfix になった finding は score 0。履歴は JSONL に残るが負債としてはカウントしない。
リポジトリの合計 debt は個別スコアの合算（finding 5件で合計 3,500 等）。

### 情報量スコア（info_score）— 0〜数千

情報理論に基づく「このバグがどれだけ驚きか」の定量化。

```
info_score = surprise × (1 + entropy) × log₂(1 + fan_out) / fix_cost × 100
```

- **surprise**: パターンの自己情報量 −log₂ P(violation)。稀なパターンほど高スコア
- **entropy**: ファイルの変更頻度から推定したバイナリエントロピー（0=安定 〜 1=高変動）
- **fan_out**: この finding が影響するファイル数（対数スケール）
- **fix_cost**: パターン別の修正コスト（0.5〜5.0）

### 解消価値（ROI）— 0〜数千

「このバグを直すとどれだけ得か」の費用対効果。影響が大きく修正が安いほど高スコア。

```
roi_score = severity × churn_weight × fan_out_weight / fix_cost × 100
```

- **churn_weight**: 0.5〜10.0（月3回以上変更で max。小規模リポでも差が出る）
- **fan_out_weight**: 1.0〜10.0（5ファイル以上が参照で max）
- **fix_cost**: パターン別の修正工数（④ガード追加=1.0, ⑩共通化=5.0 等）

### Chao1 カバレッジ推定

スキャン履歴から「まだ見つかっていない finding がどれくらいあるか」を種の豊富さ推定（Chao1）で算出。スキャンを重ねるごとにカバレッジ率が上昇。

## 設定

### プロファイル（プリセット）

スキャン設定をまとめた YAML ファイル。チームやユースケースごとに名前付きプリセットを作れます。

```bash
# ビルトインプロファイルを使う
python cli.py scan --profile deep       # 全パターン・全重大度・semantic ON
python cli.py scan -p light             # high のみ・CIゲート向け
python cli.py scan -p security          # セキュリティ特化

# CLI フラグはプロファイルより優先
python cli.py scan -p deep --severity medium
```

**優先順位**: `CLI フラグ > profile > config.json > デフォルト`

#### ビルトインプロファイル

| 名前 | 用途 | severity | semantic | 無効パターン |
|------|------|----------|----------|-------------|
| `deep` | 徹底スキャン（見逃しゼロ） | low | ON | なし |
| `light` | CI / PR レビュー向け高速チェック | high | OFF | ⑦⑧⑨⑩ |
| `security` | セキュリティ構造矛盾の重点検出 | low | OFF | ⑦⑩ |

#### カスタムプロファイルの作成

`.delta-lint/profiles/<name>.yml` を作るだけで `--profile <name>` が使えます。
ビルトインと同名なら repo-local が優先。

```yaml
# .delta-lint/profiles/onboarding.yml
name: onboarding
description: "新人向け — 詳しい説明付き"

config:
  severity: low
  semantic: true
  lang: ja

policy:
  prompt_append: |
    Report findings with detailed explanations suitable for
    someone new to this codebase. Include step-by-step reasoning.
```

```yaml
# .delta-lint/profiles/ci-gate.yml
name: ci-gate
description: "CI用 — high のみ、検証あり、失敗でブロック"

config:
  severity: high
  semantic: false

policy:
  disabled_patterns: ["⑦", "⑧", "⑨", "⑩"]
  prompt_append: |
    Only report findings you are highly confident about.
    False positives are very costly in CI context.
```

#### プロファイルのフィールド

| フィールド | 説明 |
|-----------|------|
| `name` | プロファイル名（表示用） |
| `description` | 説明（`--profile nonexistent` 時の候補一覧に使用） |
| `config.*` | CLI フラグと同じキー（severity, semantic, model, lang, backend, autofix） |
| `policy.prompt_append` | 検出プロンプトに追加する指示（constraints.yml の prompt_append と結合） |
| `policy.disabled_patterns` | 無効化するパターン（例: `["⑦", "⑩"]`） |
| `policy.exclude_paths` | スキャン対象外パス（例: `["vendor/*"]`） |
| `policy.architecture` | LLM に渡す設計文脈（誤検出削減） |
| `policy.project_rules` | プロジェクト固有のドメイン知識 |

### config.json（基本設定）

リポジトリルートに `.delta-lint/config.json` を配置することで、デフォルト動作をカスタマイズできます。
全フィールド省略可。**CLI フラグが常に config より優先**されます。

### 基本設定

```json
{
  "lang": "ja",
  "backend": "cli",
  "severity": "medium",
  "model": "claude-sonnet-4-20250514",
  "verbose": false,
  "semantic": false,
  "persona": "engineer",
  "autofix": false
}
```

| キー | 型 | デフォルト | 説明 |
|------|----|-----------|------|
| `lang` | `"ja"` \| `"en"` | `"en"` | 出力言語 |
| `backend` | `"cli"` \| `"api"` | `"cli"` | LLM バックエンド。`cli` = claude CLI（$0）、`api` = Anthropic API（従量課金） |
| `severity` | `"high"` \| `"medium"` \| `"low"` | `"high"` | 表示する最小重要度 |
| `model` | string | `"claude-sonnet-4-20250514"` | 検出に使用する Claude モデル |
| `verbose` | boolean | `false` | 詳細ログを出力 |
| `semantic` | boolean | `false` | 意味検索（暗黙の仮定抽出）を有効化。精度が上がるがスキャン時間が増加 |
| `persona` | `"engineer"` \| `"pm"` \| `"qa"` | config.json 優先 | 出力ペルソナ。CLI 未指定時は config.json の値を使用 |
| `autofix` | boolean | `false` | 検出した矛盾に対する自動修正コード生成を有効化 |

### スコアリング設定

`config.json` の `"scoring"` セクションで重みをチーム単位でカスタマイズ可能。

```json
{
  "scoring": {
    "severity_weight": { "high": 1.0, "medium": 0.6, "low": 0.3 },
    "pattern_weight": { "①": 1.0, "④": 1.0, "⑦": 0.3 },
    "status_multiplier": { "found": 1.0, "merged": 0.0 },
    "fix_cost": { "④": 0.8, "⑩": 2.0 }
  }
}
```

デフォルト値の確認・エクスポート：

```bash
python cli.py config init                 # config.json にデフォルト値を書き出し
python cli.py config init --no-interactive  # 対話なしでデフォルト書き出し
python cli.py config show                 # 現在の設定（デフォルト + オーバーライド）を表示
```

## CLI コマンド一覧

### scan

```bash
python cli.py scan [OPTIONS]
```

| フラグ | デフォルト | 説明 |
|--------|-----------|------|
| `--repo` | `.` | 対象リポジトリ |
| `--scope` | `diff` | 広さ: `diff`（変更ファイル）/ `smart`（git履歴優先）/ `all`（全ファイル） |
| `--depth` | `1hop` | 深さ: `1hop`（import依存）/ `graph`（契約グラフ解析） |
| `--lens` | `default` | 質: `default`（構造矛盾検査）/ `stress`（ストレステスト）/ `security`（セキュリティ） |
| `--profile` / `-p` | なし | スキャンプロファイル（`deep`, `light`, `security` 等） |
| `--files` | (git diff) | スキャン対象ファイルを直接指定 |
| `--docs` | なし | ドキュメントを仕様契約として含む（引数なしで自動発見） |
| `--diff-target` | `HEAD` | 差分比較先の git ref |
| `--severity` | `high` | 表示する最小重要度 |
| `--format` | `markdown` | 出力形式（`markdown` / `json`） |
| `--lang` | `en` | 出力言語（`en` / `ja`） |
| `--for` | config.json 優先 | ペルソナ（`engineer` / `pm` / `qa`） |
| `--backend` | `cli` | LLM バックエンド（`cli` / `api`） |
| `--model` | `claude-sonnet-4-20250514` | LLM モデル |
| `--semantic` | off | 意味検索を有効化 |
| `--watch` | off | ファイル変更監視モード |
| `--watch-interval` | `3.0` | ウォッチモードのポーリング間隔（秒） |
| `--autofix` | off | 修正コード自動生成 |
| `--no-verify` | off | Phase 2 検証をスキップ（高速化、FP率上昇） |
| `--no-cache` | off | キャッシュを使わず常に LLM 呼び出し |
| `--no-learn` | off | sibling_map 自動更新をスキップ |
| `--deep-workers` | `4` | 深層スキャンの並列 LLM 検証ワーカー数 |
| `--baseline` | なし | ベースラインとの差分のみ報告 |
| `--baseline-save` | off | 現在の結果をベースラインとして保存 |
| `--diff-only` | off | diff 内ファイルに関連する finding のみ表示 |
| `--dry-run` | off | LLM を呼ばずコンテキストのみ表示 |

### findings

```bash
python cli.py findings <subcommand> [OPTIONS]
```

| サブコマンド | 説明 |
|-------------|------|
| `add` | finding を手動記録 |
| `list` | finding 一覧（`--status`, `--type`, `--repo-name`, `--format json` でフィルタ可） |
| `update <id> <status>` | ステータス更新（例: `update abc123 merged`） |
| `search <query>` | キーワード検索 |
| `stats` | サマリー統計（件数、severity 別、debt_score 合計） |
| `enrich` | git churn / fan-out データで findings をエンリッチ |
| `verify-top` | 優先度上位 1/3 の findings を LLM で再検証 |
| `index` | `_index.md` を再生成 |
| `dashboard` | HTML ダッシュボードを生成してブラウザで表示 |

### suppress

```bash
python cli.py suppress [OPTIONS]
```

| フラグ | 説明 |
|--------|------|
| `<N>` | 直近スキャンの N 番目の finding を抑制 |
| `--list` | 抑制リスト表示 |
| `--check` | 期限切れの抑制をチェック |
| `--why` | 抑制理由（非対話モード用） |
| `--why-type` | 理由タイプ: `domain`/`d`, `technical`/`t`, `preference`/`p` |
| `--approved-by` | 承認者名（未指定 = 未承認 = 自己判断） |

### その他

| コマンド | 説明 |
|---------|------|
| `init` | リポジトリの初期化（構造分析 + sibling_map 生成） |
| `view` | ダッシュボードをブラウザで表示（`--regenerate` で再生成） |
| `config init` | デフォルト設定を `.delta-lint/config.json` に書き出し（`--no-interactive` 対応） |
| `config show` | 現在の設定を表示 |
| `debt-loop` | 優先度順に finding を修正（branch → fix → commit → PR） |

## Autofix / Debt Loop

### scan --autofix

スキャン結果に対して最小限の修正コードを LLM が生成し、ローカルに適用します。

```bash
python cli.py scan --repo /path/to/repo --autofix
```

### debt-loop

findings の優先度スコア順に自動修正を生成。1 finding = 1 branch = 1 PR。

```bash
# ドライラン（修正生成のみ、commit/push しない）
python cli.py debt-loop --repo /path/to/repo --dry-run -v

# 上位3件を処理
python cli.py debt-loop --repo /path/to/repo -n 3

# 特定の finding のみ
python cli.py debt-loop --ids abc123,def456
```

優先度: `info_score + roi_score + severity_bonus`（高い順に処理）

## GitHub Actions

```yaml
- uses: your-org/delta-lint@v1
  with:
    anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
    mode: "suggest"           # "review" / "suggest" / "autofix"
    severity: "high"
    fail_on_findings: "true"
```

| 入力 | デフォルト | 説明 |
|------|-----------|------|
| `anthropic_api_key` | （必須） | Anthropic API キー |
| `mode` | `"review"` | `review`（コメントのみ）/ `suggest`（インライン提案）/ `autofix`（自動コミット） |
| `severity` | `"high"` | 最小報告重要度 |
| `model` | `"claude-sonnet-4-20250514"` | 使用モデル |
| `max_diff_files` | `20` | 変更ファイル数がこれを超えるとスキップ |
| `comment_on_clean` | `false` | findings 0件でもコメント投稿 |
| `fail_on_findings` | `false` | findings 検出時にワークフローを失敗させる |

## ディレクトリ構造

DeltaLint はリポジトリ内に `.delta-lint/` ディレクトリを作成します：

```
.delta-lint/
├── config.json              # 設定（スコアリング重み含む）
├── suppress.yml             # 抑制した findings
├── sibling_map.yml          # 学習済みの兄弟ファイルマップ
├── scan_history.jsonl       # スキャン履歴（Chao1 推定用）
├── profiles/                # カスタムスキャンプロファイル（--profile で使用）
│   └── {name}.yml
├── findings/                # 検出バグの追跡記録（JSONL、append-only）
│   ├── {repo-name}.jsonl
│   └── _index.md
└── landmine_map.json        # 地雷マップ（リスクヒートマップ）
```

## ライセンス

MIT
