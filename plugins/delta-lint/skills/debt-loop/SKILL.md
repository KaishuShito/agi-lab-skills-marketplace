---
name: debt-loop
user-invocable: false
description: >
  Debt scoring, fix generation, and PR submission.
  Prioritizes findings by scoring.py の計算結果, generates fixes,
  and creates one branch + PR per finding (with pre-commit regression check).
  Triggers on "負債解消", "バグ直して", "findings直して", "自動修正", "採点して",
  "優先度つけて", "PR出して", "Issue出して", or similar.
  Requires delta-scan findings first.
compatibility: Python 3.11+, git, gh CLI. macOS/Linux/Windows.
metadata:
  author: karesansui-u
  version: 0.5.0
---

# debt-loop: Debt Scoring & Fix & PR

confirmed findings を優先度順に処理し、finding ごとにブランチ→修正→デグレチェック→PR を自動実行する。

## Critical Rules

- **必ず `debt_loop.py` を使うこと。** 手動でブランチ作成・commit・push・PR を個別に実行しない。パイプライン全体が `debt_loop.py` に実装されている。
- **PR/コミットに Co-Authored-By 行や「Generated with Claude Code」等のブランディングを入れない**（グローバルポリシー）。
- **Issue/PR の送信先は常に `origin`（自分のリポ）。** フォーク元（upstream）に送るのはユーザーが明示指示した場合のみ。

## Prerequisites

- Python 3.11+, git, gh CLI（認証済み）
- delta-scan findings が存在すること（先に `/delta-scan` を実行）
- ワーキングディレクトリがクリーンであること

## Workflow

### PR作成（confirmed findings → 自動PR）

```bash
cd ~/.claude/skills/delta-lint/scripts && python debt_loop.py --repo <REPO_PATH> --status confirmed -v
```

**これ1行で以下が全自動実行される。手動でステップを実行しないこと。**

1. confirmed findings を優先度順にソート
2. finding ごとに: ブランチ作成 → fix生成 → 適用 → **デグレチェック** → commit → push → PR
3. デグレチェックで high finding → 自動ブロック（修正を revert してスキップ）
4. 全件完了後、ベースブランチ（main）に自動復帰

### スコア確認のみ（dry-run）

```bash
cd ~/.claude/skills/delta-lint/scripts && python debt_loop.py --repo <REPO_PATH> --dry-run -v
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--repo` | `.` | Target git repository path |
| `--count` / `-n` | 3 | Number of findings to process |
| `--ids` | (none) | Comma-separated finding IDs to fix (overrides priority) |
| `--model` | claude-sonnet-4-20250514 | LLM model for fix generation |
| `--backend` | cli | `cli` ($0) or `api` (pay-per-use) |
| `--status` | found,confirmed | Statuses to include |
| `--base-branch` | (current) | Base branch for fix branches |
| `--dry-run` | false | Generate fixes + show diff only (no commit/push/PR) |
| `--verbose` / `-v` | false | Show progress |
| `--json` | false | JSON output |

## Triggers

| ユーザー発話 | 動作 |
|-------------|------|
| `delta scan --autofix` | scan 内で confirmed 全件を自動PR |
| 「PR出して」「Issue出して」 | confirmed 全件を自動PR |
| 「採点して」「優先度つけて」 | dry-run（スコア表示のみ） |
| `--ids F001,F002` | 指定 findings のみ処理 |
