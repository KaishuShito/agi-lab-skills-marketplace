---
name: delta-lint
description: >
  Detect structural contradictions in codebases using LLM analysis.
  Use when user says "delta-lint", "delta scan", "delta init",
  "structural contradiction", "構造矛盾", "デグレチェック",
  "suppress finding", "suppress check", "地雷マップ", "landmine map",
  or asks to check code consistency across files.
  Supports init, scan, suppress add/list/check workflows.
---

# delta-lint: Structural Contradiction Detector

delta-lint detects structural contradictions between source code modules — places where one module's assumptions contradict another module's behavior. It is NOT a linter for style or bugs, but a design-level conflict detector.

## Prerequisites

- Python 3.11+
- `anthropic` package (`pip install anthropic`)
- `ANTHROPIC_API_KEY` environment variable set (or in `技術的負債定量化PJT/.env`)
- Optional: `pyyaml` for suppress.yml (falls back to JSON format)

## Script Location

All scripts are in: `scripts/` (relative to this skill folder).
The absolute path is: `~/.claude/skills/delta-lint/scripts/`.
The prompt template is at: `scripts/prompts/detect.md`.

## Critical: Exit Code Interpretation

**exit code 1 from `cli.py scan` means high-severity findings were detected — this is NOT an error.**
Only treat it as an error if stderr contains a Python traceback or "Error:" prefix.

---

## Workflow 0: Init (`delta init`)

Initialize delta-lint for a repository. Creates a landmine map (risk heatmap) and enables automatic risk awareness.

**Trigger**: User says "delta init", "地雷マップ作って", "initialize delta-lint", or similar.

### Step 1: Check if already initialized

```bash
ls {repo_path}/.delta-lint/stress-test/results.json 2>/dev/null
```

- If exists: Tell user "このリポは初期化済みです。再実行しますか？" and wait for confirmation.
- If not: Proceed.

### Step 2: Run stress-test (background)

```bash
cd ~/.claude/skills/delta-lint/scripts && python stress_test.py --repo "{repo_path}" --parallel 10 --verbose --visualize 2>&1
```

**Run this in background** (`run_in_background: true`). Set timeout to 600000 (10 min).

The stress-test is fully autonomous:
- Auto-determines modification count from repo size (no `--n` needed)
- Saves checkpoints every 10 scans (results.json + landmine_map.html)
- Generates focused modifications on discovered hotspots after initial broad scan
- Auto-converges when no new files are discovered

Tell the user:
- "stress-test をバックグラウンドで開始しました"
- "10件ごとにマップが更新されます。完了したら通知します"
- "この間、通常の作業を続けて大丈夫です"

### Step 3: Add guard rules to CLAUDE.md

While the stress-test runs, add the following to the **project-level** CLAUDE.md (`{repo_path}/CLAUDE.md`). Create the file if it doesn't exist. Append to the end if it already exists.

```markdown

## delta-lint guard
- .delta-lint/stress-test/results.json に地雷マップデータがある
- ファイルを編集する前に、そのファイルの risk_score を確認すること
  - 確認方法: `python -c "import json; d=json.load(open('.delta-lint/stress-test/results.json')); risks={r['modification']['file']: len(r['findings']) for r in d['results'] if r['findings']}; print(risks.get('TARGET_FILE', 'not in map'))"` (TARGET_FILE を実際のパスに置換)
- risk_score > 0.35 のファイルを編集する場合、ユーザーに警告すること
- 大きな機能追加・リファクタリング後は delta-lint scan の実行を提案すること
```

### Step 4: When stress-test completes

When the background task finishes:
1. Read the output to get the summary (e.g., "86/100 modifications triggered 222 findings")
2. Open the heatmap: `open {repo_path}/.delta-lint/stress-test/landmine_map.html`
3. Report to user:
   - Total scans, hit rate, findings count
   - Top 3 highest-risk files with their risk scores
   - "地雷マップが完成しました。以降、高リスクファイルの編集時に自動で警告します"

### If stress-test fails

If the background task fails:
1. Read stderr output to diagnose
2. Common issues:
   - `claude -p failed`: Claude CLI not available → suggest `--backend api`
   - Timeout: Repo too large → suggest `--n 30` to reduce scope
   - Git error: Not a git repo → tell user to run from a git repository
3. Report the error and suggest recovery

---

## Workflow 1: Scan (`/delta-lint` or `/delta-lint scan`)

### Step 1: Determine scope and dry-run

Determine the target repo path (default: current working directory).
Run dry-run first to show what will be sent to the LLM:

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py scan --repo "{repo_path}" --dry-run --verbose 2>&1
```

If no changed files are found, suggest `--files` to specify files manually.

### Step 2: Confirm with user before LLM call

Present the dry-run summary to the user:
- Number of target files and dependency files
- Total context size in characters (~4 chars/token)
- Estimated cost: context_chars / 4 * input_price_per_token

**Ask the user to confirm before proceeding.** If context exceeds 60K chars, warn that results may degrade and suggest narrowing with `--files`.

### Step 3: Run the actual scan

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py scan --repo "{repo_path}" --verbose --severity {severity} 2>&1
```

Set Bash timeout to 300000 (5 min) — LLM calls can be slow.

Common options:
- `--severity high` (default) / `medium` / `low`
- `--files path/a.ts path/b.ts` — specific files instead of git diff
- `--diff-target HEAD` — git ref to diff against
- `--format json` — machine-readable output
- `--model claude-sonnet-4-20250514` — detection model (default)
- `--semantic` — enable semantic search (see below)

### Step 4: Interpret exit code

| Exit code | Meaning | Action |
|-----------|---------|--------|
| 0 | No high-severity findings | Report clean result |
| 1 + no traceback | High-severity findings found | Proceed to Step 5 (this is normal) |
| 1 + traceback | Script error | Report error, check stderr |
| Other | Unexpected | Report full output |

### Step 5: Explain results to user

Parse the Markdown output and present each finding with:
1. The pattern number and name (see "6 Contradiction Patterns" below)
2. Which two files/locations are in conflict
3. A brief explanation of why this is a problem
4. Your assessment: does this look like a true positive or false positive?

For findings tagged with `[EXPIRED SUPPRESS]`:
- Explain that this was previously suppressed but the code has changed
- Recommend the user review whether the contradiction still applies

### Step 6: Offer next actions

Based on the results, suggest:
- **If findings exist**: "suppress したい finding があれば番号を教えてください（例: `/delta-lint suppress 3`）"
- **If expired suppressions exist**: "期限切れの suppress があります。再確認して re-suppress するか、対応を検討してください"
- **If no findings**: Report clean result and mention suppressed/filtered counts if any

---

## Workflow 2: Suppress Add (`/delta-lint suppress {number}`)

### Step 1: Validate finding number

The user provides a finding number (1-based, as shown in scan output).
If the user hasn't run a scan in this session, warn them and suggest scanning first.

### Step 2: Collect reason from user

Ask the user for both fields BEFORE running the command (stdin is unavailable to the script):

- **why_type**: Which category?
  - `domain` — intentional design decision (business logic requires this)
  - `technical` — known limitation (accepted for now, may fix later)
  - `preference` — style/preference choice (team agreed on this)
- **why**: Reason for suppression
  - English: minimum 20 characters
  - Japanese: minimum 10 characters
  - Must be a meaningful explanation, not just "false positive"

### Step 3: Run suppress command (non-interactive)

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py suppress {number} --repo "{repo_path}" --why "{why_text}" --why-type "{why_type}" 2>&1
```

**Shell escaping**: If `why_text` contains quotes or special characters, escape them properly or use single-quote wrapping.

### Step 4: Confirm result

- Success: show the suppress ID (8-char hex) and confirm it was written to `.delta-lint/suppress.yml`
- Duplicate: if already suppressed, inform the user and show the existing entry ID
- Validation error: show the specific error and ask user to correct

---

## Workflow 3: Suppress List (`/delta-lint suppress --list`)

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py suppress --list --repo "{repo_path}" 2>&1
```

Present each entry with: ID, pattern, files, why_type, why, date.

---

## Workflow 4: Suppress Check (`/delta-lint suppress --check`)

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py suppress --check --repo "{repo_path}" 2>&1
```

If expired entries found:
1. List each expired entry with the hash change
2. Explain: "コードが変更されたため、suppress が期限切れになりました"
3. Suggest: re-scan to see if the contradiction still exists, then re-suppress or fix

---

## Error Handling

| Error | Likely Cause | Recovery |
|-------|-------------|----------|
| `ANTHROPIC_API_KEY not set` | Environment variable missing | Ask user to set it or check `.env` file |
| `No changed source files found` | Clean git status | Suggest `--files` to specify files manually |
| `ModuleNotFoundError: anthropic` | Package not installed | `pip install anthropic` |
| `Connection error` / timeout | Network issue | Retry once, then report |
| `Context limit reached` | Too many files/deps | Narrow scope with `--files` |
| Python traceback in stderr | Bug in delta-lint | Report the traceback to the user |

---

## Argument Reference

See [suppress-design.md](references/suppress-design.md) for the full suppress mechanism design.

### Scan
| Flag | Default | Description |
|------|---------|-------------|
| `--repo` | `.` | Repository path |
| `--files` | (git diff) | Specific files to scan |
| `--severity` | `high` | Minimum severity: high/medium/low |
| `--format` | `markdown` | Output format: markdown/json |
| `--model` | `claude-sonnet-4-20250514` | Detection model |
| `--diff-target` | `HEAD` | Git ref to diff against |
| `--dry-run` | false | Show context only |
| `--verbose` | false | Detailed progress |
| `--log-dir` | `.delta-lint/` | Log directory |
| `--semantic` | false | Enable semantic search beyond import-based 1-hop |

### Suppress
| Flag | Default | Description |
|------|---------|-------------|
| `{number}` | - | Finding number (1-based) |
| `--repo` | `.` | Repository path |
| `--list` | false | List all suppressions |
| `--check` | false | Check for expired entries |
| `--scan-log` | (latest) | Path to scan log file |
| `--why` | - | Reason for suppression (non-interactive) |
| `--why-type` | - | domain/technical/preference (non-interactive) |

---

## 6 Contradiction Patterns

When explaining findings, use these pattern descriptions:

| # | Name | Signal |
|---|------|--------|
| 1 | **Asymmetric Defaults** | Input/output paths handle the same value differently |
| 2 | **Semantic Mismatch** | Same name means different things in different modules |
| 3 | **External Spec Divergence** | Implementation contradicts the spec it claims to follow |
| 4 | **Guard Non-Propagation** | Validation present in one path, missing in a parallel path |
| 5 | **Paired-Setting Override** | Independent-looking settings secretly interfere |
| 6 | **Lifecycle Ordering** | Execution order assumption breaks under specific code paths |
