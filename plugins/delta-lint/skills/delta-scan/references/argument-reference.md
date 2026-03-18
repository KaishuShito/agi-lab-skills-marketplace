# Argument Reference

## Scan

| Flag | Default | Description |
|------|---------|-------------|
| `--repo` | `.` | Repository path |
| `--profile` / `-p` | (none) | Scan profile: `deep`, `light`, `security`, or custom name from `.delta-lint/profiles/` |
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

## Scan Profiles

Named presets that bundle scan settings. Place YAML files in `.delta-lint/profiles/` or use built-in profiles.

```bash
python cli.py scan --profile deep       # All patterns, all severities, semantic ON
python cli.py scan -p light             # High only, fast CI gate
python cli.py scan -p security          # Security-focused detection
python cli.py scan -p deep --severity medium  # CLI flag overrides profile
```

Priority: `CLI flags > profile > config.json > defaults`

### Built-in profiles

| Name | severity | semantic | Disabled patterns |
|------|----------|----------|-------------------|
| `deep` | low | ON | none |
| `light` | high | OFF | ⑦⑧⑨⑩ |
| `security` | low | OFF | ⑦⑩ |

### Custom profiles

Create `.delta-lint/profiles/<name>.yml`:

```yaml
name: my-team
description: "Team-specific scan settings"
config:
  severity: medium
  semantic: true
  lang: ja
policy:
  prompt_append: |
    Focus on authentication and data validation patterns.
  disabled_patterns: ["⑦", "⑩"]
```

### Profile fields — config

All CLI flags can be set as profile config. CLI flags always win over profile values.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `severity` | `high` / `medium` / `low` | `high` | Minimum severity to display |
| `model` | string | `claude-sonnet-4-20250514` | Detection model |
| `backend` | `cli` / `api` | `cli` | LLM backend |
| `lang` | `en` / `ja` | `en` | Output language |
| `semantic` | bool | `false` | Enable semantic search |
| `autofix` | bool | `false` | Auto-generate fixes |
| `verbose` | bool | `false` | Detailed progress |
| `diff_target` | string | `HEAD` | Git ref to diff against |
| `output_format` | `markdown` / `json` | `markdown` | Output format |
| `no_learn` | bool | `false` | Disable sibling_map auto-learning |
| `no_cache` | bool | `false` | Skip cache, always call LLM |
| `no_verify` | bool | `false` | Skip Phase 2 verification |
| `max_context_chars` | int | `80000` | Max context chars sent to LLM |
| `max_file_chars` | int | `30000` | Max chars per file (truncated) |
| `max_deps_per_file` | int | `5` | Max dependency files per target |
| `min_confidence` | float | `0.50` | Min confidence for dependency inclusion |

### Profile fields — policy

Controls detection logic. Merged with `.delta-lint/constraints.yml` at runtime.

| Key | Type | Description |
|-----|------|-------------|
| `prompt_append` | string | Extra instructions appended to detection prompt |
| `detect_prompt` | string | Override detection prompt entirely (file path or inline text) |
| `disabled_patterns` | list | Patterns to skip (e.g. `["⑦", "⑩"]`) |
| `severity_overrides` | map | Per-pattern severity remap (e.g. `{"④": "high"}`) |
| `exclude_paths` | list | Glob patterns for files to skip |
| `architecture` | list | Architectural context for LLM |
| `project_rules` | list | Domain knowledge for LLM |
| `accepted` | list | Rules for known-acceptable differences |
| `scoring_weights` | map | Override scoring formula weights (severity, pattern, fix_cost, etc.) |
| `dashboard_template` | string | Custom findings dashboard HTML template path |
| `debt_budget` | number | Max active debt score (CI gate threshold) |

See `scripts/profiles/_reference.yml` for a complete annotated example.

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
