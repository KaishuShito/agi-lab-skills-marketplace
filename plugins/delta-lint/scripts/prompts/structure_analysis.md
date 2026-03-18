You are analyzing a codebase to understand its module structure, implicit constraints, and development patterns.

Given the following list of source files (with the first 50 lines of each) and optionally git history data, produce a structural analysis.

## Output Format

Output ONLY a JSON object with this structure:

```json
{
  "modules": [
    {
      "path": "src/auth/login.ts",
      "role": "Handles user authentication and session creation",
      "key_exports": ["login", "validateToken", "SessionConfig"],
      "dependencies": ["src/db/users.ts", "src/config/auth.ts"],
      "implicit_constraints": [
        "Assumes session timeout matches config.SESSION_TTL",
        "Expects user.status to be 'active' before token generation"
      ]
    }
  ],
  "hotspots": [
    {
      "path": "src/auth/login.ts",
      "reason": "Central auth logic with many implicit constraints + frequent bug fixes"
    }
  ],
  "dev_patterns": [
    {
      "area": "src/auth/",
      "pattern": "bug-prone",
      "evidence": "12 fix commits in 6 months, concentrated in login.ts and session.ts",
      "risk": "High churn + frequent fixes suggest fragile implicit contracts"
    },
    {
      "area": "src/api/v2/",
      "pattern": "expanding",
      "evidence": "8 feat commits adding new endpoints, 3 new files in last 2 months",
      "risk": "New features may not follow patterns established in v1 — check consistency"
    }
  ]
}
```

### dev_patterns.pattern values:
- `"bug-prone"`: Many fix/bugfix commits → fragile area, implicit contracts likely broken repeatedly
- `"expanding"`: Many feat/add commits → new code being added, risk of inconsistency with existing patterns
- `"refactoring"`: Many refactor/cleanup commits → code in transition, old and new patterns may coexist
- `"stable"`: Few changes → low risk, but may become stale if dependencies evolve
- `"single-owner"`: Only one author → knowledge silo risk, implicit constraints undocumented

## Guidelines

- Focus on IMPLICIT constraints — things that are assumed but not enforced by types or contracts
- Identify hotspots: files with many cross-module dependencies or fragile assumptions
- Keep descriptions concise (one sentence each)
- List only the most important 3-5 implicit constraints per file
- For hotspots, prioritize files that would break other modules if modified
- IMPORTANT: "path" must be the full relative path from the repository root (e.g. "src/auth/login.ts", "wp-content/themes/mytheme/functions.php"), NOT just the filename
- **Git history integration**: If git history is provided below, use it to:
  - Boost hotspot ranking for files with high churn AND many fix commits
  - Identify `dev_patterns` per directory (bug-prone, expanding, refactoring, stable, single-owner)
  - Consider co-change patterns: files changed together likely share implicit contracts
  - "Expanding" areas are where future contradictions are most likely to appear

## Source Files
