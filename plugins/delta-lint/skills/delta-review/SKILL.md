---
name: delta-review
description: >
  Pre-implementation impact analysis and design review for code changes.
  Analyzes affected files, implicit contracts, and potential contradictions
  before writing code. Use when user says "delta review", "delta-review",
  "delta plan", "影響範囲チェック", "事前チェック", "impact analysis", or similar.
  Auto-triggers when user proposes code changes and .delta-lint/ exists.
compatibility: Python 3.11+, git. macOS/Linux/Windows.
metadata:
  author: karesansui-u
  version: 0.3.0
---

# delta-review: Pre-Implementation Impact Analysis

Analyzes the impact of proposed code changes before implementation. Identifies affected files, implicit contracts between modules, and potential structural contradictions that could arise from the change.

## Prerequisites

- `.delta-lint/` must exist (run `/delta-init` first)
- Python 3.11+, git

## Script Location

All scripts are in: `scripts/` (relative to the plugin root).

## When This Triggers

1. User explicitly says "delta review", "delta plan", "影響範囲", "事前チェック", etc.
2. **Auto-trigger**: User proposes any code change (new feature, bug fix, refactoring, performance improvement, etc.) AND `.delta-lint/` exists in the repo.

## Workflow

| Step | What it does |
|------|-------------|
| Affected files | Identify files impacted by the proposed change |
| Implicit contracts | Analyze assumptions between affected modules |
| Pre-scan | Auto-run delta scan on high-risk files |
| Design review | Background sub-agent reviews design decisions |
| Proposal | Present implementation plan with risks and recommendations |

Reference: [workflow-plan.md](references/workflow-plan.md)

## Quick Reference

- **6 Contradiction Patterns**: [patterns.md](references/patterns.md)
