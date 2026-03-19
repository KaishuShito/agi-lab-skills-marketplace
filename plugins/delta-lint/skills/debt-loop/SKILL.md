---
name: debt-loop
user-invocable: false
status: draft
description: >
  [Draft — CLI統合未完了] Debt scoring and fix generation.
  Prioritizes findings by scoring.py の計算結果, generates local fixes, shows diff.
  Does NOT commit, push, or create PRs.
  Triggers on "負債解消", "バグ直して", "findings直して", "自動修正", "採点して",
  "優先度つけて", or similar. Requires delta-scan findings first.
compatibility: Python 3.11+, git. macOS/Linux/Windows.
metadata:
  author: karesansui-u
  version: 0.3.0-draft
---

# debt-loop: Debt Scoring & Fix Generation

> **⚠️ Draft**: このスキルは設計段階。`cli.py` への統合は未完了。
> debt_loop.py は存在するが、cli.py の subcommand としては未登録。

Prioritizes delta-lint findings by score, generates fix code, and shows
a local diff. **Commits, push, PR は一切行わない。**

## Scope — what this skill does and does NOT do

| Does | Does NOT |
|------|----------|
| 優先度スコア計算（scoring.py に準拠） | git commit |
| 修正コード生成（LLM） | git push |
| ローカル diff 表示 | PR 作成 |
| findings ステータス更新 | ブランチ作成 |

ユーザーが「コミットして」「PR出して」「pushして」と言った場合は、
**このスキルの範囲外**。Claude の通常のgit操作として処理する。

> **PR/コミット作成時の注意**: Co-Authored-By 行や「Generated with Claude Code」等のブランディングを入れないこと（グローバルポリシー）。

## Prerequisites

- Python 3.11+, git
- delta-scan findings must exist (run `/delta-scan` first)
- claude CLI (for $0 fix generation) or ANTHROPIC_API_KEY

## Script Location

All scripts are in: `scripts/` (relative to the plugin root).
Entry point: `debt_loop.py` (also available as `cli.py debt-loop`)

## Execution Policy

- **Do NOT ask for confirmation** — cli backend is $0.
- **Always run with `--dry-run`** — generate fixes and show diff only.
- **NEVER commit, push, or create PRs** from this skill.

## Workflow

### 1. Determine target repo

If the user specifies a repo path, use it. Otherwise use the current working directory (check for `.delta-lint/findings/` to confirm findings exist).

### 2. Run the loop

```bash
cd <plugin-root>/scripts
python cli.py debt-loop --repo <REPO_PATH> --dry-run -v [OPTIONS]
```

Or directly:
```bash
python debt_loop.py --repo <REPO_PATH> --dry-run -v [OPTIONS]
```

### 3. Options

| Flag | Default | Description |
|------|---------|-------------|
| `--repo` | `.` | Target git repository path |
| `--count` / `-n` | 3 | Number of findings to process |
| `--ids` | (none) | Comma-separated finding IDs to fix (overrides priority) |
| `--model` | claude-sonnet-4-20250514 | LLM model for fix generation |
| `--backend` | cli | `cli` ($0) or `api` (pay-per-use) |
| `--status` | found,verified | Statuses to include |
| `--dry-run` | true | Generate fixes but don't commit/push/PR (スキルからは常に true で呼ぶ) |
| `--verbose` / `-v` | false | Show progress |
| `--json` | false | JSON output |

### 4. What it does (per finding)

1. Scores finding: `priority = info_score + roi_score + severity_bonus`
2. Generates fix via LLM (using FindingContext for source code)
3. Shows diff of proposed changes
4. Reverts changes (dry-run)
5. Moves to next finding

### 5. Priority scoring

> **注意**: スコア計算の唯一の正はコード（`scoring.py` + `info_theory.py`）。
> 以下は概要のみ。詳細や重みが変わった場合はコードが正。

`priority = info_score + roi_score + severity_bonus`

- **info_score**: surprise × entropy × fan_out 等から算出（`info_theory.py`）
- **roi_score**: severity × churn × fan_out / fix_cost 等から算出（`scoring.py`）
- **severity_bonus**: high=300, medium=100, low=0

### Routing logic

1. User says "debt-loop", "採点して", "優先度つけて" → Score + dry-run fixes
2. User specifies `--ids` → Process only those specific findings
3. Default: top 3 by priority score
