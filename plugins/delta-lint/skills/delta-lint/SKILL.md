---
name: delta-lint
description: >
  Autonomous code quality agent that detects structural contradictions in codebases,
  generates fixes, and creates GitHub Issues + PRs — all without additional user input.
  Use when user says "delta-lint", "delta scan", "delta fix", "構造矛盾", "デグレチェック",
  or asks to check code consistency across files.
user-invocable: true
---

# delta-lint: Autonomous Structural Contradiction Agent

delta-lint is an autonomous agent that detects structural contradictions between source code modules, generates fixes, and creates GitHub Issues and Pull Requests — all from a single command.

A structural contradiction occurs when one module's assumptions contradict another module's behavior. These are design-level conflicts that static linters cannot catch.

## What Makes This Autonomous

1. **One command, full workflow**: User provides a repo path or diff — agent handles everything else
2. **Self-recovering**: If detection finds no issues with default settings, automatically widens scope
3. **Self-researching**: Reads related files, traces imports, and gathers context without being asked
4. **End-to-end delivery**: Detection → Analysis → Fix generation → Testing → Issue + PR creation

## Prerequisites

- Python 3.11+
- `anthropic` package (`pip install anthropic`)
- `ANTHROPIC_API_KEY` environment variable set
- `gh` CLI authenticated (for Issue/PR creation)
- Git repository as target

## Script Location

All scripts are in: `scripts/` (relative to this skill's plugin folder).
The prompt template is at: `scripts/prompts/detect.md`.

## Critical: Exit Code Interpretation

**exit code 1 from `cli.py scan` means high-severity findings were detected — this is NOT an error.**
Only treat it as an error if stderr contains a Python traceback or "Error:" prefix.

---

## Workflow: Full Autonomous Run (`/delta-lint` or `/delta-lint <repo_path>`)

This is the primary workflow. The agent runs through all phases autonomously.

### Phase 1: Scan — Detect structural contradictions

#### Step 1.1: Determine scope

Determine the target repo path (argument or current working directory).
Run dry-run first:

```bash
cd {skill_scripts_dir} && python cli.py scan --repo "{repo_path}" --dry-run --verbose 2>&1
```

If no changed files found, try `--diff-target HEAD~5` to widen scope.
If still nothing, suggest `--files` to specify files manually.

#### Step 1.2: Cost check and confirm

Present the dry-run summary to the user:
- Number of target files and dependency files
- Total context size in characters (~4 chars/token)
- Estimated cost: context_chars / 4 * $0.003/1K (Sonnet input) + ~$0.015/1K (output)

**Ask the user to confirm before proceeding.** If context exceeds 60K chars, warn and suggest narrowing.

#### Step 1.3: Run detection

```bash
cd {skill_scripts_dir} && python cli.py scan --repo "{repo_path}" --verbose --severity {severity} 2>&1
```

Set Bash timeout to 300000 (5 min).

Common options:
- `--severity high` (default) / `medium` / `low`
- `--files path/a.ts path/b.ts` — specific files
- `--diff-target HEAD~N` — wider git diff range
- `--format json` — machine-readable output
- `--model claude-sonnet-4-20250514` — detection model

#### Step 1.4: Interpret exit code

| Exit code | Meaning | Action |
|-----------|---------|--------|
| 0 | No high-severity findings | Report clean, offer to widen scope |
| 1 + no traceback | High-severity findings found | Proceed to Phase 2 |
| 1 + traceback | Script error | Report error, check stderr |

#### Step 1.5: Triage findings

For each finding, assess:
1. Pattern number and name (see "6 Contradiction Patterns" below)
2. Which two files/locations are in conflict
3. True positive vs false positive assessment
4. Fixability: can this be fixed without breaking other behavior?

Present findings to the user with your assessment. Ask which findings to fix.

### Phase 2: Fix — Generate and validate corrections

For each confirmed finding:

#### Step 2.1: Deep analysis

Read both files involved in the contradiction fully. Trace the data flow to understand:
- What the correct behavior should be
- Which side of the contradiction is wrong
- What the minimal fix is

#### Step 2.2: Generate fix

Apply the minimal code change using Edit tool. Rules:
- Fix only the contradiction — do not refactor surrounding code
- Preserve existing code style (indentation, naming conventions)
- If the fix is ambiguous, present options to the user

#### Step 2.3: Validate

Run the project's existing tests:
```bash
# Detect test framework from package.json, Cargo.toml, etc.
# Run relevant tests
```

If tests fail, analyze the failure and adjust the fix. If tests pass, proceed.

### Phase 3: Deliver — Create Issue and PR

#### Step 3.1: Create GitHub Issue

```bash
gh issue create --title "{concise title}" --body "$(cat <<'EOF'
## Structural Contradiction Detected by delta-lint

**Pattern**: {pattern_name}
**Severity**: {severity}

### Description
{contradiction_description}

### Location
- **File A**: `{file_a}` — {detail_a}
- **File B**: `{file_b}` — {detail_b}

### Impact
{impact_description}

### Suggested Fix
{fix_description}

---
Detected by [delta-lint](https://github.com/karesansui-u/agi-lab-skills-marketplace) — autonomous structural contradiction detector
EOF
)"
```

#### Step 3.2: Create branch and PR

```bash
git checkout -b fix/{short-description}
git add {changed_files}
git commit -m "$(cat <<'EOF'
fix: {description}

{detailed_explanation}

Detected by delta-lint (Pattern {pattern})
Closes #{issue_number}
EOF
)"
git push -u origin fix/{short-description}

gh pr create --title "fix: {description}" --body "$(cat <<'EOF'
## Summary
{what_was_wrong_and_how_it_was_fixed}

## Structural Contradiction
- **Pattern**: {pattern_name}
- **File A**: `{file_a}`
- **File B**: `{file_b}`

## Test plan
- [ ] Existing tests pass
- [ ] New test covers the contradiction case

Closes #{issue_number}
Detected by [delta-lint](https://github.com/karesansui-u/agi-lab-skills-marketplace)
EOF
)"
```

#### Step 3.3: Report results

Present a summary:
- Findings detected (with pattern names)
- Fixes applied
- Issue and PR URLs
- Any findings skipped and why

---

## Workflow: Scan Only (`/delta-lint scan`)

Runs only Phase 1 (detection + triage). Does not generate fixes or PRs.
Follow Steps 1.1 through 1.5 above.

After presenting findings, offer:
- "修正も行いますか？"
- "suppress したい finding があれば番号を教えてください"

---

## Workflow: Suppress (`/delta-lint suppress {number}`)

### Step 1: Validate finding number

If no scan in this session, warn and suggest scanning first.

### Step 2: Collect reason

Ask for:
- **why_type**: `domain` / `technical` / `preference`
- **why**: Minimum 20 chars EN / 10 chars JA

### Step 3: Run suppress

```bash
cd {skill_scripts_dir} && python cli.py suppress {number} --repo "{repo_path}" --why "{why_text}" --why-type "{why_type}" 2>&1
```

### Step 4: Confirm

Show suppress ID and confirm it was written to `.delta-lint/suppress.yml`.

---

## 6 Contradiction Patterns

| # | Name | Signal |
|---|------|--------|
| 1 | **Asymmetric Defaults** | Input/output paths handle the same value differently |
| 2 | **Semantic Mismatch** | Same name means different things in different modules |
| 3 | **External Spec Divergence** | Implementation contradicts the spec it claims to follow |
| 4 | **Guard Non-Propagation** | Validation present in one path, missing in a parallel path |
| 5 | **Paired-Setting Override** | Independent-looking settings secretly interfere |
| 6 | **Lifecycle Ordering** | Execution order assumption breaks under specific code paths |

## Error Handling

| Error | Likely Cause | Recovery |
|-------|-------------|----------|
| `ANTHROPIC_API_KEY not set` | Env var missing | Ask user to set it |
| `No changed source files found` | Clean git status | Widen with `--diff-target HEAD~5` or `--files` |
| `ModuleNotFoundError: anthropic` | Package not installed | `pip install anthropic` |
| `gh: not logged in` | gh CLI not authenticated | `gh auth login` |
| Context too large | Too many files | Narrow with `--files` |

## Self-Recovery Strategies

When the agent encounters obstacles:

1. **No changed files**: Automatically try wider diff range (`HEAD~3`, `HEAD~5`, `HEAD~10`)
2. **API rate limit**: Wait and retry once with exponential backoff
3. **Test failure after fix**: Analyze failure, adjust fix, re-run tests (max 2 retries)
4. **Ambiguous fix**: Present options to user with pros/cons for each
5. **gh CLI issues**: Fall back to manual instructions if GitHub operations fail
