# Suppress Mechanism Design

## Overview

The suppress mechanism allows users to mark specific findings as intentional, hiding them from future scans until the relevant code changes.

## Key Concepts

### finding_hash (Stable Identification)
- Hash of **sorted file paths + rounded line numbers**
- **NOT** based on LLM text output (which varies between runs)
- Files are sorted alphabetically for order-independence
- Line numbers rounded to 5-line buckets to absorb LLM variance of +/-4 lines
- Falls back to files-only hash when line numbers unavailable
- First 8 hex chars of SHA-256

### code_hash (Auto-Expiration)
- Hash of +/-10 lines surrounding the target location
- When code changes, code_hash mismatches -> suppress expires automatically
- Expired suppressions re-surface the finding with an "EXPIRED SUPPRESS" tag
- Prevents stale suppressions from hiding real issues after refactoring

### why_type Categories
| Type | Shortcut | Meaning |
|------|----------|---------|
| `domain` | `d` | Intentional design decision (business logic requires this) |
| `technical` | `t` | Known technical limitation (will be fixed, accepted for now) |
| `preference` | `p` | Style/preference choice (team agreed on this approach) |

### why Validation
- English: minimum 20 characters
- Japanese: minimum 10 characters (auto-detected via Unicode range)
- Forces meaningful explanation, prevents drive-by suppression

## Storage

Suppressions are stored in `.delta-lint/suppress.yml` (YAML if pyyaml installed, JSON otherwise).

Each entry contains:
```yaml
- id: "a3f2c1b8"          # = finding_hash
  finding_hash: "a3f2c1b8"
  pattern: "①"             # metadata only, NOT used for matching
  files: ["src/a.ts", "src/b.ts"]
  code_hash: "e7d4f2a1"
  why: "These modules intentionally use different defaults for backward compat"
  why_type: "domain"
  date: "2026-03-12"
  author: "sunagawa"
  line_ranges: ["37-47", "82-92"]  # optional
```

## Flow

### Suppress Add
1. User runs scan -> findings displayed with numbers
2. User runs `suppress {number}` with --why and --why-type
3. System computes finding_hash + code_hash
4. Entry saved to suppress.yml

### Suppress Check (Expiration)
1. For each entry, recompute code_hash from current files
2. If hash differs -> entry is "expired" (code changed)
3. Expired entries are reported, and their findings will re-appear in next scan

### During Scan
1. Load suppress.yml
2. For each finding, compute finding_hash and check against entries
3. Matching + valid code_hash -> suppress (hide)
4. Matching + different code_hash -> expired (re-show with warning)
5. No match -> normal severity filtering
