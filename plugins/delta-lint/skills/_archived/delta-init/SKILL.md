---
name: delta-init
description: >
  Initialize delta-lint for a repository. Creates a landmine map (risk heatmap)
  and enables automatic risk awareness. Use when user says "delta init",
  "delta-init", "地雷マップ作って", "initialize delta-lint", or similar.
compatibility: Python 3.11+. git recommended but not required. macOS/Linux/Windows.
metadata:
  author: karesansui-u
  version: 0.3.0
---

# delta-init: Initialize Landmine Map

Initializes delta-lint for the current repository. Runs a stress-test to create a landmine map (risk heatmap), detects existing structural contradictions, and adds guard rules to CLAUDE.md.

## Prerequisites

See the main delta-lint plugin for dependency details. Key requirements:
- Python 3.11+
- git（推奨。なくても動作するが精度が下がる）
- claude CLI (for $0 LLM calls via subscription) or ANTHROPIC_API_KEY

## Script Location

All scripts are in: `scripts/` (relative to the plugin root — wherever this plugin is installed).

## CRITICAL: First action on trigger

**スキルが発火した瞬間、Bash や Read より先に以下をテキスト出力する:**

```
── δ-lint ── 初期化開始
  デグレ特化型構造矛盾検出
  ストレステストを開始します...
```

**この出力の後に** workflow-init.md のステップを順に実行する。

## Workflow

Reference: [workflow-init.md](references/workflow-init.md)

| Step | What it does |
|------|-------------|
| Banner | 上記バナーを即座に出力 |
| Check | Already initialized? Ask before re-running |
| Stress-test | Background scan of entire repo (10 parallel) |
| Structure display | Show modules, hotspots, implicit constraints |
| Existing bugs | Report contradictions found in current code |
| Guard rules | Add risk awareness to CLAUDE.md |
| Heatmap | Open landmine map in browser |

## Quick Reference

- **6 Contradiction Patterns**: [patterns.md](references/patterns.md)
