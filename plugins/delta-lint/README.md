# delta-lint

**構造矛盾検出器** — コードモジュール間の暗黙の前提が破れている箇所を LLM で検出します。

スタイルやシンプルなバグではなく、**設計レベルの不整合**（あるモジュールの前提が別のモジュールの振る舞いと矛盾している箇所）を見つけます。

## セットアップ

### 必須

- **Python 3.11+**
- **git** — diff ベースのスキャンに使用

### LLM バックエンド（いずれか1つ）

| 方法 | コスト | セットアップ |
|------|--------|------------|
| **claude CLI**（推奨） | $0（サブスクリプション内） | `npm install -g @anthropic-ai/claude-code` |
| **Anthropic API** | 従量課金 | `export ANTHROPIC_API_KEY='sk-ant-...'` |

### 自動インストール

delta-lint は初回スキャン時に不足している依存を自動でインストールします：

```
claude CLI がない → npm install -g @anthropic-ai/claude-code を試行
anthropic SDK がない → pip install anthropic を試行
PyYAML がない → pip install pyyaml を試行
```

インストールが拒否された場合やエラーの場合も、利用可能な代替手段に自動フォールバックして動作を継続します。手動介入は不要です。

## 使い方

### Claude Code から（推奨）

```
> delta init          # リポジトリの初期化（地雷マップ生成）
> delta scan          # 変更ファイルのスキャン
> delta plan          # コード改修の影響範囲分析
```

### CLI から直接

```bash
cd plugins/delta-lint/scripts

# 変更ファイルをスキャン
python cli.py scan --repo /path/to/repo

# 特定ファイルをスキャン
python cli.py scan --files src/handler.ts src/router.ts

# 日本語で出力
python cli.py scan --lang ja

# APIバックエンドを使用
python cli.py scan --backend api
```

## 検出する矛盾パターン

| # | パターン | 例 |
|---|---------|---|
| ① | Asymmetric Defaults | 登録時は `null` を受け入れるが表示時は `undefined` を空文字に変換 |
| ② | Semantic Mismatch | `status: 0` がモジュール A では "pending"、B では "inactive" |
| ③ | External Spec Divergence | RFC 7230 に準拠すると書いてあるが実装が逸脱 |
| ④ | Guard Non-Propagation | create エンドポイントにはバリデーションがあるが update にはない |
| ⑤ | Paired-Setting Override | `timeout=30s` と `retries=5` の組み合わせが上流の制限を超える |
| ⑥ | Lifecycle Ordering | エラーリカバリパスでは認証ミドルウェアがルートハンドラの後に実行される |

## 設定

リポジトリルートに `.delta-lint/config.json` を配置することで、デフォルト動作をカスタマイズできます。
全フィールド省略可。**CLI フラグが常に config より優先**されます。

### 全設定項目一覧

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
| `persona` | `"engineer"` \| `"pm"` \| `"qa"` | `"engineer"` | 出力ペルソナ。`pm` = ビジネス影響ベース、`qa` = テストシナリオベース |
| `autofix` | boolean | `false` | 検出した矛盾に対する自動修正コード生成を有効化（後述） |

### 設定の変更方法

**方法 1: 直接編集**

```bash
# config.json を作成/編集
vim .delta-lint/config.json
```

**方法 2: Claude Code から（ペルソナ設定）**

```
> delta scan set-persona pm
# → .delta-lint/config.json の persona を "pm" に設定
```

**方法 3: CLI フラグで一時的に上書き**

```bash
# config.json の設定に関係なく、この実行だけ日本語 + medium 以上を表示
python cli.py scan --repo /path/to/repo --lang ja --severity medium
```

### Autofix（自動修正）

デフォルトでは**無効**です。有効にすると、検出された矛盾に対して最小限の修正コードを LLM が生成します。

**有効化:**

```json
{
  "autofix": true
}
```

**動作モード（GitHub Action）:**

| モード | 動作 | 設定場所 |
|--------|------|---------|
| `review`（デフォルト） | PR にコメントのみ | `mode: "review"` |
| `suggest` | コメント + Suggested Changes（インライン提案） | `mode: "suggest"` |
| `autofix` | 修正を自動コミット＆プッシュ | `mode: "autofix"` |

**GitHub Actions での使用例:**

```yaml
- uses: your-org/delta-lint@v1
  with:
    anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
    mode: "suggest"           # or "autofix"
    severity: "high"
    fail_on_findings: "true"
```

**注意:**
- autofix は LLM 生成のため、修正が不正確な場合があります。`suggest` モードでレビュー付き提案を使うのが安全です
- CI 上の `autofix` モードは修正コードを自動コミットするため、ブランチ保護ルールに注意してください

### GitHub Action の全入力

| 入力 | デフォルト | 説明 |
|------|-----------|------|
| `anthropic_api_key` | （必須） | Anthropic API キー |
| `mode` | `"review"` | `review` / `suggest` / `autofix` |
| `severity` | `"high"` | 最小報告重要度 |
| `model` | `"claude-sonnet-4-20250514"` | 使用モデル |
| `max_diff_files` | `20` | PR の変更ファイル数がこれを超えるとスキャンをスキップ |
| `comment_on_clean` | `false` | findings 0件でもコメントを投稿 |
| `fail_on_findings` | `false` | findings 検出時にワークフローを失敗させる |

### ディレクトリ構造

delta-lint はリポジトリ内に `.delta-lint/` ディレクトリを作成します：

```
.delta-lint/
├── config.json          # 設定（上記参照）
├── suppress.yml         # 抑制した findings
├── findings/            # 検出バグの追跡記録（JSONL）
│   └── {repo-name}.jsonl
└── landmine_map.json    # 地雷マップ（リスクヒートマップ）
```

## ライセンス

MIT
