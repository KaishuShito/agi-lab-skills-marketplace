You are generating realistic virtual code modifications for stress-testing a codebase.

Given the structural analysis and recent git history, generate {n} virtual modifications that test the codebase's resilience to change.

## Rules

1. **80% must modify existing code** (change function behavior, alter data flow, modify conditions). Max 20% new file additions.
2. **Be specific**: name the exact file, function, and line-level location. Not "modify auth module" but "change validateToken() in src/auth/login.ts to reject expired refresh tokens".
3. **No buzzwords**: no blockchain, AI/ML, quantum, or trendy tech additions. Focus on realistic business logic changes.
4. **Target hotspots**: include modifications near known fragile areas from the structural analysis.
5. **Mix categories**:
   - `similar` (50%): variations of changes that have historically occurred (based on git log)
   - `exploratory` (50%): changes that test architectural boundaries and implicit constraints

## Output Format

Output ONLY a JSON array:

```json
[
  {
    "id": 1,
    "file": "src/auth/login.ts",
    "function": "validateToken",
    "description": "Change token expiry check from < to <= so tokens expiring at exact boundary are rejected",
    "category": "similar",
    "rationale": "Git log shows 3 token-related fixes; boundary conditions are a known weak point",
    "affected_files": ["src/auth/login.ts", "src/middleware/auth.ts", "src/api/session.ts"]
  }
]
```

## Guidelines

- `affected_files` should list files that would need to be checked if this modification were made
- `rationale` explains why this modification is a good stress test
- Each modification should test a DIFFERENT implicit constraint or cross-module boundary
- Prefer modifications that could cause SILENT failures (wrong results, not crashes)

## Structural Analysis

{structure}

## Recent Git History

{git_log}
