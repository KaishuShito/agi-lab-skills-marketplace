You are generating FOCUSED virtual code modifications for stress-testing a codebase.

Previous rounds of stress-testing have identified hotspots and patterns. Generate {n} NEW modifications that dig deeper into these areas.

## Rules

1. **Target the hotspots**: Focus modifications on or near the files listed in the hotspot analysis below.
2. **Don't repeat**: The "already tested" list shows modifications that have been run. Generate DIFFERENT modifications that test NEW angles on the same areas.
3. **Be specific**: name the exact file, function, and line-level location.
4. **No buzzwords**: no blockchain, AI/ML, quantum, or trendy tech additions.
5. **Test cross-module interactions**: modifications should probe the boundaries between hotspot files and their dependents.

## Output Format

Output ONLY a JSON array:

```json
[
  {
    "id": 1,
    "file": "src/auth/login.ts",
    "function": "validateToken",
    "description": "Change token expiry check from < to <= so tokens expiring at exact boundary are rejected",
    "category": "focused",
    "rationale": "Previous scan found 3 contradictions here; testing a different angle",
    "affected_files": ["src/auth/login.ts", "src/middleware/auth.ts"]
  }
]
```

## Hotspot Analysis (from previous rounds)

Files with highest risk scores:
{hotspots}

## Already Tested Modifications (DO NOT repeat these)

{already_tested}

## Structural Analysis

{structure}
