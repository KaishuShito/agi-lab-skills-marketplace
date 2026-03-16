#!/usr/bin/env python3
"""
delta-lint GitHub Action entrypoint.

Runs structural contradiction detection on PR changed files
and posts findings as a PR comment.
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


def get_pr_changed_files() -> list[str]:
    """Get changed files from the PR using GitHub event data."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise RuntimeError("GITHUB_EVENT_PATH not set — not running in GitHub Actions?")

    with open(event_path) as f:
        event = json.load(f)

    pr_number = event.get("pull_request", {}).get("number")
    if not pr_number:
        raise RuntimeError("No pull_request in event — action must be triggered by pull_request event")

    repo = os.environ["GITHUB_REPOSITORY"]
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls/{pr_number}/files",
         "--paginate", "--jq", ".[].filename"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get PR files: {result.stderr}")

    return [f for f in result.stdout.strip().split("\n") if f]


def filter_scannable_files(files: list[str]) -> list[str]:
    """Filter to source files that delta-lint can analyze."""
    from retrieval import filter_source_files
    return filter_source_files(files)


def run_scan(files: list[str], severity: str, model: str) -> dict:
    """Run delta-lint scan and return parsed results."""
    from retrieval import build_context
    from detector import detect
    from output import filter_findings
    from suppress import load_suppressions

    repo_path = os.environ.get("GITHUB_WORKSPACE", ".")

    context = build_context(repo_path, files)
    if not context.target_files:
        return {"findings": [], "filtered": 0, "suppressed": 0}

    findings = detect(context, repo_name=os.environ.get("GITHUB_REPOSITORY", ""),
                      model=model, backend="api")

    suppressions = load_suppressions(repo_path)
    result = filter_findings(findings, min_severity=severity,
                             suppressions=suppressions, repo_path=repo_path)

    return {
        "findings": result.shown,
        "filtered": len(result.filtered),
        "suppressed": len(result.suppressed),
        "expired": len(result.expired),
    }


def format_comment(scan_result: dict, files: list[str], severity: str) -> str:
    """Format scan results as a PR comment in Markdown."""
    findings = scan_result["findings"]
    filtered = scan_result["filtered"]
    suppressed = scan_result["suppressed"]
    expired = scan_result.get("expired", 0)

    lines = []
    lines.append("## 🔍 delta-lint: Structural Contradiction Report\n")

    if not findings:
        lines.append(f"No structural contradictions detected in {len(files)} file(s).")
        if suppressed:
            lines.append(f"\n*({suppressed} suppressed)*")
        if filtered:
            lines.append(f"\n*({filtered} below `{severity}` severity, filtered)*")
        return "\n".join(lines)

    severity_icon = {"high": "🔴", "medium": "🟡", "low": "⚪"}

    lines.append(f"**{len(findings)} contradiction(s)** found in {len(files)} file(s).\n")

    for i, f in enumerate(findings, 1):
        if f.get("parse_error"):
            lines.append(f"### {i}. ⚠️ Parse Error")
            lines.append(f"```\n{f.get('raw_response', 'N/A')[:300]}\n```\n")
            continue

        pattern = f.get("pattern", "?")
        sev = f.get("severity", "medium").lower()
        icon = severity_icon.get(sev, "⚪")

        expired_tag = ""
        if f.get("_expired_suppress"):
            expired_tag = " ⏰ *expired suppress*"

        lines.append(f"### {i}. {icon} Pattern {pattern} ({sev}){expired_tag}\n")

        loc = f.get("location", {})
        if isinstance(loc, dict):
            file_a = loc.get("file_a", "?")
            file_b = loc.get("file_b", "?")
            lines.append(f"**File A**: `{file_a}`")
            if loc.get("detail_a"):
                lines.append(f"> {loc['detail_a']}")
            lines.append(f"**File B**: `{file_b}`")
            if loc.get("detail_b"):
                lines.append(f"> {loc['detail_b']}")
            lines.append("")

        if f.get("contradiction"):
            lines.append(f"**Contradiction**: {f['contradiction']}\n")
        if f.get("impact"):
            lines.append(f"**Impact**: {f['impact']}\n")

    # Footer
    footer_parts = []
    if filtered:
        footer_parts.append(f"{filtered} below `{severity}` severity")
    if suppressed:
        footer_parts.append(f"{suppressed} suppressed")
    if expired:
        footer_parts.append(f"{expired} expired suppressions (re-shown)")

    if footer_parts:
        lines.append(f"---\n*{', '.join(footer_parts)}*\n")

    lines.append("<sub>Powered by <a href=\"https://github.com/sunagawa-agi/agi-lab-skills-marketplace/tree/main/plugins/delta-lint\">delta-lint</a> — structural contradiction detector</sub>")

    return "\n".join(lines)


def post_or_update_comment(body: str) -> int | None:
    """Post or update a delta-lint comment on the PR. Returns comment ID."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    with open(event_path) as f:
        event = json.load(f)

    pr_number = event["pull_request"]["number"]
    repo = os.environ["GITHUB_REPOSITORY"]
    marker = "<!-- delta-lint-comment -->"
    body_with_marker = f"{marker}\n{body}"

    # Find existing delta-lint comment
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{pr_number}/comments",
         "--paginate", "--jq", f'.[] | select(.body | startswith("{marker}")) | .id'],
        capture_output=True, text=True, timeout=30,
    )

    existing_id = result.stdout.strip().split("\n")[0] if result.stdout.strip() else None

    if existing_id:
        # Update existing comment
        result = subprocess.run(
            ["gh", "api", "--method", "PATCH",
             f"repos/{repo}/issues/comments/{existing_id}",
             "-f", f"body={body_with_marker}"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            print(f"Updated existing comment {existing_id}", file=sys.stderr)
            return int(existing_id)
    else:
        # Create new comment
        result = subprocess.run(
            ["gh", "api", "--method", "POST",
             f"repos/{repo}/issues/{pr_number}/comments",
             "-f", f"body={body_with_marker}"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            comment = json.loads(result.stdout)
            comment_id = comment.get("id")
            print(f"Created comment {comment_id}", file=sys.stderr)
            return comment_id

    print(f"Failed to post comment: {result.stderr}", file=sys.stderr)
    return None


def set_output(name: str, value: str):
    """Set a GitHub Actions output variable."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{name}={value}\n")


def main():
    parser = argparse.ArgumentParser(description="delta-lint GitHub Action entrypoint")
    parser.add_argument("--severity", default="high")
    parser.add_argument("--model", default="claude-sonnet-4-20250514")
    parser.add_argument("--max-diff-files", type=int, default=20)
    parser.add_argument("--comment-on-clean", default="false")
    args = parser.parse_args()

    # 1. Get PR changed files
    print("Getting PR changed files...", file=sys.stderr)
    all_files = get_pr_changed_files()
    print(f"  PR has {len(all_files)} changed file(s)", file=sys.stderr)

    if len(all_files) > args.max_diff_files:
        print(f"  Skipping: {len(all_files)} files exceeds max_diff_files ({args.max_diff_files})",
              file=sys.stderr)
        set_output("findings_count", "0")
        return

    # 2. Filter to scannable source files
    source_files = filter_scannable_files(all_files)
    print(f"  {len(source_files)} source file(s) to scan", file=sys.stderr)

    if not source_files:
        print("  No source files to scan", file=sys.stderr)
        set_output("findings_count", "0")
        return

    # 3. Run scan
    print(f"Running delta-lint scan (model={args.model}, severity>={args.severity})...",
          file=sys.stderr)
    scan_result = run_scan(source_files, args.severity, args.model)
    findings_count = len(scan_result["findings"])
    print(f"  {findings_count} finding(s)", file=sys.stderr)

    set_output("findings_count", str(findings_count))

    # 4. Post comment
    if findings_count > 0 or args.comment_on_clean == "true":
        comment_body = format_comment(scan_result, source_files, args.severity)
        comment_id = post_or_update_comment(comment_body)
        if comment_id:
            set_output("comment_id", str(comment_id))

    # 5. Exit code (1 = findings found, for use with fail_on_findings)
    if findings_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
