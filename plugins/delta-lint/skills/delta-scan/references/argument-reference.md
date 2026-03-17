# Argument Reference

## Scan

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
| `--backend` | `cli` | LLM backend: `cli` (claude -p, $0) or `api` (SDK, pay-per-use) |
| `--lang` | `en` | Output language for findings: `en` (English) or `ja` (Japanese) |

## Suppress

| Flag | Default | Description |
|------|---------|-------------|
| `{number}` | - | Finding number (1-based) |
| `--repo` | `.` | Repository path |
| `--list` | false | List all suppressions |
| `--check` | false | Check for expired entries |
| `--scan-log` | (latest) | Path to scan log file |
| `--why` | - | Reason for suppression (non-interactive) |
| `--why-type` | - | domain/technical/preference (non-interactive) |

## Findings

| Flag | Default | Description |
|------|---------|-------------|
| `--repo` | `.` | Base path for `.delta-lint/findings/` |
| `--repo-name` | - | Repository name (`owner/repo` format) |
| `--file` | - | File path of the finding |
| `--line` | - | Line number |
| `--type` | `bug` | `bug` / `contradiction` / `suspicious` / `enhancement` |
| `--finding-severity` | `medium` | `high` / `medium` / `low` |
| `--pattern` | - | Contradiction pattern (①〜⑥) |
| `--title` | - | Short title |
| `--description` | - | Detailed description |
| `--status` | `found` | `found` / `verified` / `submitted` / `merged` / `rejected` / `wontfix` / `duplicate` |
| `--url` | - | GitHub Issue/PR URL |
| `--found-by` | - | Who found it (`claude-opus` etc.) |
| `--verified` | false | Code-verified flag |
| `--format` | `text` | Output format for list/stats: `text` / `json` |

## Configuration File

Place `.delta-lint/config.json` in the repo root to set defaults. CLI flags always override config values.

```json
{
  "lang": "ja",
  "backend": "cli",
  "severity": "medium",
  "model": "claude-sonnet-4-20250514",
  "verbose": false,
  "semantic": false
}
```

All fields are optional — only include what you want to override.

| Key | Type | Description |
|-----|------|-------------|
| `lang` | `"en"` \| `"ja"` | Output language for finding descriptions |
| `backend` | `"cli"` \| `"api"` | LLM backend |
| `severity` | `"high"` \| `"medium"` \| `"low"` | Minimum severity to display |
| `model` | string | Detection model |
| `verbose` | bool | Detailed progress output |
| `semantic` | bool | Enable semantic search |
