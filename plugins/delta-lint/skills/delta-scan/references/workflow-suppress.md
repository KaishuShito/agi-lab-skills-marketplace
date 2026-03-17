# Workflow 2/3/5: Suppress

## Workflow 2: Suppress Add (`/delta-lint suppress {number}`)

### Step 1: Validate finding number

The user provides a finding number (1-based, as shown in scan output).
If the user hasn't run a scan in this session, warn them and suggest scanning first.

### Step 2: Collect reason from user

Ask the user for both fields BEFORE running the command (stdin is unavailable to the script):

- **why_type**: Which category?
  - `domain` — intentional design decision (business logic requires this)
  - `technical` — known limitation (accepted for now, may fix later)
  - `preference` — style/preference choice (team agreed on this)
- **why**: Reason for suppression
  - English: minimum 20 characters
  - Japanese: minimum 10 characters
  - Must be a meaningful explanation, not just "false positive"

### Step 3: Run suppress command (non-interactive)

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py suppress {number} --repo "{repo_path}" --why "{why_text}" --why-type "{why_type}" 2>&1
```

**Shell escaping**: If `why_text` contains quotes or special characters, escape them properly or use single-quote wrapping.

### Step 4: Confirm result

- Success: show the suppress ID (8-char hex) and confirm it was written to `.delta-lint/suppress.yml`
- Duplicate: if already suppressed, inform the user and show the existing entry ID
- Validation error: show the specific error and ask user to correct

---

## Workflow 3: Suppress List (`/delta-lint suppress --list`)

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py suppress --list --repo "{repo_path}" 2>&1
```

Present each entry with: ID, pattern, files, why_type, why, date.

---

## Workflow 5: Suppress Check (`/delta-lint suppress --check`)

```bash
cd ~/.claude/skills/delta-lint/scripts && python cli.py suppress --check --repo "{repo_path}" 2>&1
```

If expired entries found:
1. List each expired entry with the hash change
2. Explain: "コードが変更されたため、suppress が期限切れになりました"
3. Suggest: re-scan to see if the contradiction still exists, then re-suppress or fix

See also: [suppress-design.md](suppress-design.md) for the full suppress mechanism design.
