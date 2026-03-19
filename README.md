# DeltaLint — Autonomous Structural Contradiction Agent

リポジトリURLを渡すだけで、コードの構造矛盾を検出し、修正PRの作成まで自律的に実行するClaude Code plugin。

## これは何？

DeltaLint は、ソースコード内の**構造矛盾**（あるモジュールの前提が別のモジュールの振る舞いと矛盾している箇所）を検出する自律エージェントです。

静的リンターでは検出できない、設計レベルの矛盾を6つのパターンで分類・検出します。

### 自律性のポイント

- **一度お願いしたら最後まで**: diff取得 → 矛盾検出 → 修正コード生成 → テスト実行 → Issue起票 → PR作成
- **エラーから自分で立て直す**: 変更ファイルがなければ自動でdiff範囲を広げる、テスト失敗なら修正を調整してリトライ
- **足りない情報を自分で調べる**: import先を自動追跡し、関連モジュールのコンテキストを収集

### 実績

promptfoo（GitHub 16K star）で DeltaLint が検出した構造矛盾のPRがマージされました。

## インストール

```bash
# Claude Code で実行
/plugin marketplace add karesansui-u/agi-lab-skills-marketplace
/plugin install delta-lint@delta-lint-marketplace
```

### 前提条件

- Python 3.11+
- `pip install anthropic`
- `ANTHROPIC_API_KEY` 環境変数をセット
- `gh` CLI で GitHub 認証済み

## 使い方

### 基本（フル自律実行）

```
/delta-lint /path/to/repo
```

渡すだけで、以下を自動実行します：
1. git diffから変更ファイルを特定
2. import追跡で関連モジュールを収集
3. LLMで構造矛盾を検出（6パターン）
4. 検出結果をトリアージ（真陽性/偽陽性の評価）
5. 修正コードを生成・テスト実行
6. GitHub Issueを起票
7. 修正PRを作成

### スキャンのみ

```
/delta-lint scan
```

検出のみ実行し、修正・PR作成は行いません。

### 特定ファイルを指定

```
/delta-lint scan --files src/auth.ts src/session.ts
```

### Suppress（意図的な矛盾を除外）

```
/delta-lint suppress 3
/delta-lint suppress --list
/delta-lint suppress --check
```

## 6つの構造矛盾パターン

| # | パターン名 | シグナル |
|---|-----------|---------|
| 1 | **Asymmetric Defaults** | 入力パスと出力パスで同じ値の扱いが異なる |
| 2 | **Semantic Mismatch** | 同じ名前が異なるモジュールで異なる意味を持つ |
| 3 | **External Spec Divergence** | 実装が準拠するはずの仕様と矛盾 |
| 4 | **Guard Non-Propagation** | バリデーションが一方のパスにあり、並行パスにない |
| 5 | **Paired-Setting Override** | 独立に見える設定が実は干渉し合う |
| 6 | **Lifecycle Ordering** | 実行順序の前提が特定のコードパスで崩れる |

## Repository Structure

```text
.claude-plugin/
└── marketplace.json

plugins/
└── delta-lint/
    ├── .claude-plugin/
    │   └── plugin.json
    ├── scripts/
    │   ├── cli.py
    │   ├── detector.py
    │   ├── retrieval.py
    │   ├── output.py
    │   ├── suppress.py
    │   └── prompts/
    │       └── detect.md
    └── skills/
        └── delta-lint/
            └── SKILL.md
```

## License

MIT
