You are analyzing a codebase to understand its module structure and implicit constraints.

Given the following list of source files (with the first 50 lines of each), produce a structural analysis.

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
      "reason": "Central auth logic with many implicit constraints"
    }
  ]
}
```

## Guidelines

- Focus on IMPLICIT constraints — things that are assumed but not enforced by types or contracts
- Identify hotspots: files with many cross-module dependencies or fragile assumptions
- Keep descriptions concise (one sentence each)
- List only the most important 3-5 implicit constraints per file
- For hotspots, prioritize files that would break other modules if modified
- IMPORTANT: "path" must be the full relative path from the repository root (e.g. "src/auth/login.ts", "wp-content/themes/mytheme/functions.php"), NOT just the filename

## Source Files
