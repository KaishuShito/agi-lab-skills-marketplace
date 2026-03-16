#!/usr/bin/env python3
"""
delta-lint GitHub Action entrypoint.

Modes:
  review   — Post findings as a PR comment (default)
  suggest  — Post findings with GitHub Suggested Changes inline
  autofix  — Generate fixes, commit, and push to the PR branch
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# delta-lint scripts directory (sibling to action/)
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def get_event() -> dict:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise RuntimeError("GITHUB_EVENT_PATH not set — not running in GitHub Actions?")
    with open(event_path) as f:
        return json.load(f)


def get_pr_number() -> int:
    return get_event()["pull_request"]["number"]


def get_pr_head_ref() -> str:
    return get_event()["pull_request"]["head"]["ref"]


def get_repo() -> str:
    return os.environ["GITHUB_REPOSITORY"]


def gh_api(method: str, endpoint: str, **fields) -> dict | str:
    """Call GitHub API via gh cli."""
    cmd = ["gh", "api", "--method", method, endpoint]
    for k, v in fields.items():
        cmd.extend(["-f", f"{k}={v}"])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"gh api {method} {endpoint} failed: {result.stderr}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout


def get_pr_changed_files() -> list[str]:
    """Get changed files from the PR."""
    repo = get_repo()
    pr_number = get_pr_number()
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls/{pr_number}/files",
         "--paginate", "--jq", ".[].filename"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get PR files: {result.stderr}")
    return [f for f in result.stdout.strip().split("\n") if f]


def set_output(name: str, value: str):
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{name}={value}\n")


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def filter_scannable_files(files: list[str]) -> list[str]:
    from retrieval import filter_source_files
    return filter_source_files(files)


def run_scan(files: list[str], severity: str, model: str) -> dict:
    from retrieval import build_context
    from detector import detect
    from output import filter_findings
    from suppress import load_suppressions

    repo_path = os.environ.get("GITHUB_WORKSPACE", ".")
    context = build_context(repo_path, files)
    if not context.target_files:
        return {"findings": [], "filtered": 0, "suppressed": 0, "expired": 0,
                "context": context}

    findings = detect(context, repo_name=get_repo(), model=model, backend="api")
    suppressions = load_suppressions(repo_path)
    result = filter_findings(findings, min_severity=severity,
                             suppressions=suppressions, repo_path=repo_path)

    return {
        "findings": result.shown,
        "filtered": len(result.filtered),
        "suppressed": len(result.suppressed),
        "expired": len(result.expired),
        "context": context,
    }


# ---------------------------------------------------------------------------
# Fix generation (for suggest + autofix modes)
# ---------------------------------------------------------------------------

FIX_PROMPT = """\
You are a code fix generator. Given a structural contradiction between two files,
generate the minimal fix to resolve it.

## Rules
- Fix ONLY the contradiction described. Do not refactor or improve other code.
- Prefer fixing the file that deviates from the established pattern/config.
- Output valid JSON only.

## Contradiction
{contradiction_json}

## Source files
{source_code}

## Output Format
Return a JSON array of fixes. Each fix:
```json
[
  {{
    "file": "path/to/file.py",
    "line": 8,
    "old_code": "exact line(s) to replace",
    "new_code": "replacement line(s)",
    "explanation": "brief explanation of the fix"
  }}
]
```

If the fix requires changes in multiple places, include multiple entries.
If you cannot generate a safe fix, return `[]`.
"""


def generate_fixes(findings: list[dict], context, model: str) -> list[dict]:
    """Call Claude to generate fixes for each finding."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=api_key)

    all_fixes = []
    source_code = context.to_prompt_string() if hasattr(context, 'to_prompt_string') else ""

    for finding in findings:
        if finding.get("parse_error"):
            continue

        prompt = FIX_PROMPT.format(
            contradiction_json=json.dumps(finding, indent=2, ensure_ascii=False),
            source_code=source_code,
        )

        try:
            message = client.messages.create(
                model=model,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text
            fixes = _parse_fixes(raw)
            for fix in fixes:
                fix["_finding"] = finding
            all_fixes.extend(fixes)
        except Exception as e:
            print(f"  Fix generation failed for finding: {e}", file=sys.stderr)

    return all_fixes


def _parse_fixes(raw: str) -> list[dict]:
    """Parse fix JSON from LLM response."""
    text = raw.strip()
    if "```" in text:
        import re
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass

    bracket_start = text.find("[")
    bracket_end = text.rfind("]")
    if bracket_start >= 0 and bracket_end > bracket_start:
        try:
            parsed = json.loads(text[bracket_start:bracket_end + 1])
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
    return []


# ---------------------------------------------------------------------------
# Mode: review (comment only)
# ---------------------------------------------------------------------------

def format_review_comment(scan_result: dict, files: list[str],
                          severity: str, mode: str) -> str:
    findings = scan_result["findings"]
    filtered = scan_result["filtered"]
    suppressed = scan_result["suppressed"]
    expired = scan_result.get("expired", 0)

    lines = []
    lines.append(f"## 🔍 delta-lint: Structural Contradiction Report\n")

    if not findings:
        lines.append(f"No structural contradictions detected in {len(files)} file(s).")
        if suppressed:
            lines.append(f"\n*({suppressed} suppressed)*")
        if filtered:
            lines.append(f"\n*({filtered} below `{severity}` severity, filtered)*")
        return "\n".join(lines)

    severity_icon = {"high": "🔴", "medium": "🟡", "low": "⚪"}
    lines.append(f"**{len(findings)} contradiction(s)** found in {len(files)} file(s)."
                 f" Mode: `{mode}`\n")

    for i, f in enumerate(findings, 1):
        if f.get("parse_error"):
            lines.append(f"### {i}. ⚠️ Parse Error")
            lines.append(f"```\n{f.get('raw_response', 'N/A')[:300]}\n```\n")
            continue

        pattern = f.get("pattern", "?")
        sev = f.get("severity", "medium").lower()
        icon = severity_icon.get(sev, "⚪")
        expired_tag = " ⏰ *expired suppress*" if f.get("_expired_suppress") else ""

        lines.append(f"### {i}. {icon} Pattern {pattern} ({sev}){expired_tag}\n")

        loc = f.get("location", {})
        if isinstance(loc, dict):
            lines.append(f"**File A**: `{loc.get('file_a', '?')}`")
            if loc.get("detail_a"):
                lines.append(f"> {loc['detail_a']}")
            lines.append(f"**File B**: `{loc.get('file_b', '?')}`")
            if loc.get("detail_b"):
                lines.append(f"> {loc['detail_b']}")
            lines.append("")

        if f.get("contradiction"):
            lines.append(f"**Contradiction**: {f['contradiction']}\n")
        if f.get("impact"):
            lines.append(f"**Impact**: {f['impact']}\n")

    footer_parts = []
    if filtered:
        footer_parts.append(f"{filtered} below `{severity}` severity")
    if suppressed:
        footer_parts.append(f"{suppressed} suppressed")
    if expired:
        footer_parts.append(f"{expired} expired suppressions (re-shown)")
    if footer_parts:
        lines.append(f"---\n*{', '.join(footer_parts)}*\n")

    lines.append("<sub>Powered by <a href=\"https://github.com/karesansui-u/agi-lab-skills-marketplace/tree/main/plugins/delta-lint\">delta-lint</a></sub>")
    return "\n".join(lines)


def post_or_update_comment(body: str) -> int | None:
    repo = get_repo()
    pr_number = get_pr_number()
    marker = "<!-- delta-lint-comment -->"
    body_with_marker = f"{marker}\n{body}"

    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{pr_number}/comments",
         "--paginate", "--jq",
         f'.[] | select(.body | startswith("{marker}")) | .id'],
        capture_output=True, text=True, timeout=30,
    )
    existing_id = result.stdout.strip().split("\n")[0] if result.stdout.strip() else None

    if existing_id:
        resp = gh_api("PATCH", f"repos/{repo}/issues/comments/{existing_id}",
                       body=body_with_marker)
        print(f"Updated existing comment {existing_id}", file=sys.stderr)
        return int(existing_id)
    else:
        resp = gh_api("POST", f"repos/{repo}/issues/{pr_number}/comments",
                       body=body_with_marker)
        comment_id = resp.get("id") if isinstance(resp, dict) else None
        print(f"Created comment {comment_id}", file=sys.stderr)
        return comment_id


# ---------------------------------------------------------------------------
# Mode: suggest (PR review with suggested changes)
# ---------------------------------------------------------------------------

def post_suggestions(findings: list[dict], fixes: list[dict], scan_result: dict,
                     files: list[str], severity: str):
    """Post a PR review with inline suggested changes."""
    repo = get_repo()
    pr_number = get_pr_number()

    # Build review comments with suggestions
    review_comments = []
    for fix in fixes:
        file_path = fix.get("file", "")
        line = fix.get("line", 1)
        new_code = fix.get("new_code", "")
        explanation = fix.get("explanation", "")
        finding = fix.get("_finding", {})

        pattern = finding.get("pattern", "?")
        sev = finding.get("severity", "?")
        contradiction = finding.get("contradiction", "")

        body = (
            f"**delta-lint** Pattern {pattern} ({sev})\n\n"
            f"{contradiction}\n\n"
            f"{explanation}\n\n"
            f"```suggestion\n{new_code}\n```"
        )

        comment = {
            "path": file_path,
            "line": line,
            "body": body,
        }

        # Multi-line suggestion: add start_line if old_code spans multiple lines
        old_code = fix.get("old_code", "")
        old_lines = old_code.strip().split("\n")
        if len(old_lines) > 1:
            comment["start_line"] = max(1, line - len(old_lines) + 1)

        review_comments.append(comment)

    if not review_comments:
        # No fixes generated — fall back to review comment
        comment_body = format_review_comment(scan_result, files, severity, "suggest")
        comment_body += "\n\n> ⚠️ Could not generate suggested changes for these findings."
        post_or_update_comment(comment_body)
        return

    # Also post summary comment
    summary = format_review_comment(scan_result, files, severity, "suggest")
    n_fixes = len(review_comments)
    summary += f"\n\n✏️ **{n_fixes} suggested change(s)** posted as inline review comments."
    post_or_update_comment(summary)

    # Post PR review with suggestions
    review_body = {
        "body": f"delta-lint found {len(findings)} contradiction(s). "
                f"Suggested {n_fixes} fix(es) below.",
        "event": "COMMENT",
        "comments": review_comments,
    }

    # Use gh api with raw JSON input
    result = subprocess.run(
        ["gh", "api", "--method", "POST",
         f"repos/{repo}/pulls/{pr_number}/reviews",
         "--input", "-"],
        input=json.dumps(review_body),
        capture_output=True, text=True, timeout=30,
    )

    if result.returncode == 0:
        print(f"Posted PR review with {n_fixes} suggestions", file=sys.stderr)
    else:
        print(f"Failed to post PR review: {result.stderr}", file=sys.stderr)
        # Fallback: post fixes in the summary comment
        fallback = summary + "\n\n**Inline suggestions failed. Fixes below:**\n\n"
        for fix in fixes:
            fallback += f"**`{fix['file']}` line {fix.get('line', '?')}**:\n"
            fallback += f"```diff\n- {fix.get('old_code', '')}\n+ {fix.get('new_code', '')}\n```\n"
            fallback += f"{fix.get('explanation', '')}\n\n"
        post_or_update_comment(fallback)


# ---------------------------------------------------------------------------
# Mode: autofix (commit fixes to PR branch)
# ---------------------------------------------------------------------------

def apply_and_push_fixes(fixes: list[dict], scan_result: dict,
                         files: list[str], severity: str) -> int:
    """Apply fixes to files, commit, and push to the PR branch."""
    repo_path = os.environ.get("GITHUB_WORKSPACE", ".")
    applied = []

    for fix in fixes:
        file_path = fix.get("file", "")
        old_code = fix.get("old_code", "")
        new_code = fix.get("new_code", "")

        if not file_path or not old_code or not new_code:
            continue

        full_path = Path(repo_path) / file_path
        if not full_path.exists():
            print(f"  Skip: {file_path} not found", file=sys.stderr)
            continue

        content = full_path.read_text(encoding="utf-8")
        if old_code not in content:
            # Try with trailing whitespace stripped
            old_lines = "\n".join(l.rstrip() for l in old_code.split("\n"))
            content_stripped = "\n".join(l.rstrip() for l in content.split("\n"))
            if old_lines not in content_stripped:
                print(f"  Skip: old_code not found in {file_path}", file=sys.stderr)
                continue
            # Apply on stripped content
            new_content = content_stripped.replace(old_lines, new_code, 1)
            full_path.write_text(new_content + "\n", encoding="utf-8")
        else:
            new_content = content.replace(old_code, new_code, 1)
            full_path.write_text(new_content, encoding="utf-8")

        applied.append(fix)
        print(f"  Applied fix: {file_path} line {fix.get('line', '?')}", file=sys.stderr)

    if not applied:
        # No fixes applied — fall back to review comment
        comment_body = format_review_comment(scan_result, files, severity, "autofix")
        comment_body += "\n\n> ⚠️ Autofix could not apply changes. Manual review required."
        post_or_update_comment(comment_body)
        return 0

    # Configure git
    subprocess.run(
        ["git", "config", "user.name", "delta-lint[bot]"],
        cwd=repo_path, capture_output=True, timeout=10,
    )
    subprocess.run(
        ["git", "config", "user.email", "delta-lint[bot]@users.noreply.github.com"],
        cwd=repo_path, capture_output=True, timeout=10,
    )

    # Stage and commit
    for fix in applied:
        subprocess.run(
            ["git", "add", fix["file"]],
            cwd=repo_path, capture_output=True, timeout=10,
        )

    fix_summary = ", ".join(
        f"{f['file']}:{f.get('line', '?')}" for f in applied
    )
    commit_msg = f"fix: resolve structural contradictions detected by delta-lint\n\n{fix_summary}"

    result = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=repo_path, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"  Commit failed: {result.stderr}", file=sys.stderr)
        return 0

    # Push to PR branch
    head_ref = get_pr_head_ref()
    result = subprocess.run(
        ["git", "push", "origin", f"HEAD:{head_ref}"],
        cwd=repo_path, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(f"  Push failed: {result.stderr}", file=sys.stderr)
        return 0

    print(f"  Pushed {len(applied)} fix(es) to {head_ref}", file=sys.stderr)

    # Post summary comment
    comment_body = format_review_comment(scan_result, files, severity, "autofix")
    comment_body += (
        f"\n\n✅ **Autofix applied {len(applied)} change(s)** and pushed to `{head_ref}`.\n\n"
        "| File | Line | Fix |\n|------|------|-----|\n"
    )
    for fix in applied:
        comment_body += (
            f"| `{fix['file']}` | {fix.get('line', '?')} "
            f"| {fix.get('explanation', '')} |\n"
        )
    post_or_update_comment(comment_body)

    return len(applied)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="delta-lint GitHub Action")
    parser.add_argument("--mode", default="review",
                        choices=["review", "suggest", "autofix"])
    parser.add_argument("--severity", default="high")
    parser.add_argument("--model", default="claude-sonnet-4-20250514")
    parser.add_argument("--max-diff-files", type=int, default=20)
    parser.add_argument("--comment-on-clean", default="false")
    parser.add_argument("--fail-on-findings", default="false")
    args = parser.parse_args()

    # 1. Get PR changed files
    print("Getting PR changed files...", file=sys.stderr)
    all_files = get_pr_changed_files()
    print(f"  PR has {len(all_files)} changed file(s)", file=sys.stderr)

    if len(all_files) > args.max_diff_files:
        print(f"  Skipping: {len(all_files)} files exceeds limit ({args.max_diff_files})",
              file=sys.stderr)
        set_output("findings_count", "0")
        set_output("fixed_count", "0")
        return

    # 2. Filter to scannable source files
    source_files = filter_scannable_files(all_files)
    print(f"  {len(source_files)} source file(s) to scan", file=sys.stderr)

    if not source_files:
        print("  No source files to scan", file=sys.stderr)
        set_output("findings_count", "0")
        set_output("fixed_count", "0")
        return

    # 3. Run scan
    print(f"Running delta-lint scan (mode={args.mode}, model={args.model}, "
          f"severity>={args.severity})...", file=sys.stderr)
    scan_result = run_scan(source_files, args.severity, args.model)
    findings = scan_result["findings"]
    findings_count = len(findings)
    print(f"  {findings_count} finding(s)", file=sys.stderr)
    set_output("findings_count", str(findings_count))

    # 4. No findings — optional clean comment
    if findings_count == 0:
        set_output("fixed_count", "0")
        if args.comment_on_clean == "true":
            body = format_review_comment(scan_result, source_files, args.severity, args.mode)
            post_or_update_comment(body)
        return

    # 5. Mode dispatch
    fixed_count = 0

    if args.mode == "review":
        body = format_review_comment(scan_result, source_files, args.severity, "review")
        comment_id = post_or_update_comment(body)
        if comment_id:
            set_output("comment_id", str(comment_id))

    elif args.mode == "suggest":
        print("Generating fixes for suggested changes...", file=sys.stderr)
        fixes = generate_fixes(findings, scan_result.get("context"), args.model)
        print(f"  {len(fixes)} fix(es) generated", file=sys.stderr)
        post_suggestions(findings, fixes, scan_result, source_files, args.severity)
        fixed_count = len(fixes)

    elif args.mode == "autofix":
        print("Generating fixes for autofix...", file=sys.stderr)
        fixes = generate_fixes(findings, scan_result.get("context"), args.model)
        print(f"  {len(fixes)} fix(es) generated", file=sys.stderr)
        fixed_count = apply_and_push_fixes(fixes, scan_result, source_files, args.severity)

    set_output("fixed_count", str(fixed_count))

    # 6. Exit code
    if args.fail_on_findings == "true" and findings_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
