---
name: delta-review
user-invocable: false
description: >
  Pre-implementation impact analysis for code changes. Has two modes:
  (1) FULL MODE - explicit "delta review", "delta plan", "影響範囲チェック" etc.
  (2) AUTO MODE - triggers when user proposes implementation ("〇〇を実装して",
  "〇〇を追加したい", "〇〇を修正して", "これ実装できる？", "こういう機能作りたい",
  "こういう修正どうなる？", "気をつけたほうがいいことは？") AND .delta-lint/ exists.
  Auto mode is lightweight: 1-3 line risk summary, no confirmation, proceeds immediately.
compatibility: Python 3.11+, git. macOS/Linux/Windows.
metadata:
  author: karesansui-u
  version: 0.4.0
---

# delta-review: Pre-Implementation Impact Analysis

Two modes based on how it's triggered.

## Prerequisites

- `.delta-lint/` must exist (auto-created on first `/delta-scan`)
- Python 3.11+, git

## Script Location

All scripts are in: `scripts/` (relative to the plugin root).

## Mode Selection

### FULL MODE (explicit trigger)
- "delta review", "delta plan", "影響範囲チェック", "事前チェック", "impact analysis"
- → Full workflow: [workflow-plan.md](references/workflow-plan.md)

### AUTO MODE (implementation trigger)
- User proposes any code change, new feature, bug fix, refactoring, spec discussion
- Examples: 「〇〇を追加したい」「〇〇を実装して」「これ修正して」「こういう機能作りたい」「これ実装できる？」「追加するとき気をつけることは？」
- → Lightweight pre-check: [workflow-autocheck.md](references/workflow-autocheck.md)
- **確認を求めない。止めない。チェック結果を出してそのまま作業に入る。**
- 関連する地雷マップ/findings データがなければ **何も出力せず silent pass**（ユーザーの作業を止めない）

## Quick Reference

- **6 Contradiction Patterns**: [patterns.md](references/patterns.md)
