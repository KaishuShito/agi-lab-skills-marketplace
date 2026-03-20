---
name: delta-scan
description: >
  Scan for structural contradictions between source code modules. Detects places
  where one module's assumptions contradict another's behavior. Auto-initializes
  on first run (no separate init needed). Use when user says "delta scan",
  "delta-scan", "delta init", "delta-init", "構造矛盾チェック", "デグレチェック",
  "地雷マップ作って", "suppress finding", "suppress check", "findings", "バグ記録",
  "PRレビュー", "PRスキャン", "PR scan", "review PR", "scan PR", "プルリクチェック",
  or when user mentions a dl- prefixed ID (e.g. "dl-65edfb5a を調査して").
  NOT a style linter or generic bug finder.
compatibility: Python 3.11+, git. macOS/Linux/Windows.
metadata:
  author: karesansui-u
  version: 0.4.0
---

# delta-scan: Structural Contradiction Scanner

Scans changed or specified files for structural contradictions — places where one module's assumptions contradict another module's behavior. Auto-initializes on first run. Includes auto-triage, findings management, and suppress mechanism.

## Prerequisites

See the main delta-lint plugin for dependency details. Key requirements:
- Python 3.11+
- git
- claude CLI (for $0 LLM calls via subscription) or ANTHROPIC_API_KEY

## Script Location

All scripts are in: `scripts/` (relative to the plugin root).

## Critical Rules

**Exit code 1 from `cli.py scan` means high-severity findings were detected — this is NOT an error.**
Only treat it as an error if stderr contains a Python traceback or "Error:" prefix.

**NEVER use `--files` to manually select files.** Always let cli.py handle file selection. The CLI has `--since 3months` default + `--scope smart` fallback. Manually picking files bypasses this and drastically reduces scan quality.

**Scan は「記録して終わり」ではない。** findings 記録後、自動で全件の詳細調査（ソースコード精読→矛盾の実在確認→ステータス更新）まで実行する。ユーザーの指示を待たない。詳細は [workflow-scan.md Step 7](references/workflow-scan.md)。

## Workflows

| Workflow | Trigger | Reference |
|----------|---------|-----------|
| **Init** | "delta init", "地雷マップ作って", or auto on first scan | [workflow-init.md](references/workflow-init.md) |
| **Scan** | "delta scan", default | [workflow-scan.md](references/workflow-scan.md) (scan→記録→**自動調査→ステータス更新**まで一気通貫) |
| **PR Scan** | "PRレビュー", "PR scan", "review PR", "プルリクチェック" | [workflow-scan.md](references/workflow-scan.md) (PR mode) |
| **Stress Test** | "ストレステスト", "stress test", "地雷マップ更新", "--lens stress", "フルスキャン" | [workflow-stress.md](references/workflow-stress.md) |
| **Suppress Add** | "suppress {number}" | [workflow-suppress.md](references/workflow-suppress.md) |
| **Suppress List** | "suppress --list" | [workflow-suppress.md](references/workflow-suppress.md) |
| **Suppress Check** | "suppress --check" | [workflow-suppress.md](references/workflow-suppress.md) |
| **Findings** | "findings", "バグ記録" | [workflow-findings.md](references/workflow-findings.md) |

### Routing logic

1. User says "delta init", "地雷マップ作って" → **Init**（リッチ初期化体験）
2. User mentions stress/lens stress/ストレステスト/フルスキャン/地雷マップ更新/`--lens stress` → **Stress Test**（バックグラウンド実行）
3. User says "delta scan" or just `/delta-scan`（stress 以外） → **Scan**（初回なら auto-init 後に scan）
4. User mentions PR/プルリク/レビュー ("PRレビュー", "PR scan", "review PR", "プルリクスキャン", "PRチェック") → **PR Scan** (= Scan with `--scope pr`)
5. User mentions a `dl-` prefixed ID (e.g. "dl-65edfb5a 調べて") → **Investigate Finding**
6. User says "suppress" with a number → **Suppress Add**
7. User says "suppress --list" or "suppress --check" → **Suppress List/Check**
8. User says "findings" → **Findings**
9. User says "set-persona pm/qa/engineer" → **Set default persona** (no scan)
10. If unclear, default to **Scan**

### Investigate Finding (dl- ID)

When user provides a `dl-` prefixed ID:
1. Find the repo's `.delta-lint/findings/*.jsonl` — search all JSONL files for matching ID (hash suffix match also works)
2. Extract the finding's full details: file_a, file_b, pattern, title, description, severity
3. Read the actual source files mentioned in the finding
4. Assess: is the contradiction still present? has it been fixed? is it by design?
5. Report your analysis with code evidence
6. **Update finding status** based on assessment:
```bash
cd ~/.claude/skills/delta-lint/scripts && python3 -c "
from findings import update_status
update_status('{repo_path}', '{repo_name}', '{finding_id}', '{new_status}')
"
```
   - Confirmed bug → `confirmed`
   - Dead code / no callers → `wontfix`
   - Already fixed → `wontfix`
   - By design → `wontfix`
   - False positive → `false_positive`

### Time window

User mentions a time period (「1週間分」「半年」「1year」etc.) → convert to `--since`. Default: `3months`. See [workflow-scan.md Step 0.4](references/workflow-scan.md) for mapping table.

### Persona mode

Output can be tailored for different audiences via `--for`:
- `--for engineer` (default): technical output with file paths and method names
- `--for pm`: user-facing impact, spec gaps, business decisions — no code references
- `--for qa`: test scenarios and reproduction steps — no code knowledge needed

Natural language triggers: 「PM向け」「わかりやすく」→ pm, 「QA向け」「テストケースで」→ qa

See [persona-guide.md](references/persona-guide.md) for details.

## Quick Reference

- **6 Contradiction Patterns**: [patterns.md](references/patterns.md)
- **All CLI arguments**: [argument-reference.md](references/argument-reference.md)
- **Suppress mechanism design**: [suppress-design.md](references/suppress-design.md)
