---
name: delta-scan
description: >
  Scan for structural contradictions between source code modules. Detects places
  where one module's assumptions contradict another's behavior. Auto-initializes
  on first run (no separate init needed). Use when user says "delta scan",
  "delta-scan", "delta init", "delta-init", "構造矛盾チェック", "デグレチェック",
  "地雷マップ作って", "suppress finding", "suppress check", "findings", "バグ記録",
  or similar. NOT a style linter or generic bug finder.
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

## Critical: Exit Code Interpretation

**exit code 1 from `cli.py scan` means high-severity findings were detected — this is NOT an error.**
Only treat it as an error if stderr contains a Python traceback or "Error:" prefix.

## Workflows

| Workflow | Trigger | Reference |
|----------|---------|-----------|
| **Scan** | "delta scan", default | [workflow-scan.md](references/workflow-scan.md) |
| **Suppress Add** | "suppress {number}" | [workflow-suppress.md](references/workflow-suppress.md) |
| **Suppress List** | "suppress --list" | [workflow-suppress.md](references/workflow-suppress.md) |
| **Suppress Check** | "suppress --check" | [workflow-suppress.md](references/workflow-suppress.md) |
| **Findings** | "findings", "バグ記録" | [workflow-findings.md](references/workflow-findings.md) |

### Routing logic

1. User says "delta scan" or just `/delta-scan` → **Scan**
2. User says "suppress" with a number → **Suppress Add**
3. User says "suppress --list" or "suppress --check" → **Suppress List/Check**
4. User says "findings" → **Findings**
5. User says "set-persona pm/qa/engineer" → **Set default persona** (no scan)
6. If unclear, default to **Scan**

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
