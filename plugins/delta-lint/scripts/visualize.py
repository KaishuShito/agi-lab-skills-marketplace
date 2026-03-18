"""
Visualization layer for delta-lint stress-test.

Generates a single self-contained HTML file with a D3.js treemap heatmap
showing per-file risk scores from stress-test results.

No Python dependencies beyond stdlib. Output HTML uses D3.js via CDN.

Template is loaded from templates/dashboard.html and injected with data
using string.Template ($variable substitution). This keeps HTML/CSS/JS
in a proper .html file where editors can lint and highlight them.
"""

import argparse
import json
import sys
from pathlib import Path

from aggregate import aggregate_results, build_treemap_data, FileRisk


_CATEGORY_RULES: list[tuple[str, str]] = [
    # (path substring, human label)
    ("providers", "Provider Integrations"),
    ("provider", "Provider Integrations"),
    ("llm", "LLM / AI Core"),
    ("model", "Model Layer"),
    ("agent", "Agent / Orchestration"),
    ("chain", "Agent / Orchestration"),
    ("pipeline", "Pipeline"),
    ("redteam", "Security / Red Team"),
    ("security", "Security / Red Team"),
    ("auth", "Authentication"),
    ("middleware", "Middleware"),
    ("gateway", "API Gateway"),
    ("api", "API Layer"),
    ("route", "Routing"),
    ("handler", "Request Handlers"),
    ("controller", "Controllers"),
    ("service", "Business Logic"),
    ("util", "Utilities / Helpers"),
    ("helper", "Utilities / Helpers"),
    ("lib", "Libraries"),
    ("common", "Shared / Common"),
    ("shared", "Shared / Common"),
    ("core", "Core Engine"),
    ("config", "Configuration"),
    ("env", "Environment / Config"),
    ("db", "Database"),
    ("database", "Database"),
    ("store", "Data Store"),
    ("cache", "Caching"),
    ("queue", "Message Queue"),
    ("event", "Events / Hooks"),
    ("hook", "Events / Hooks"),
    ("plugin", "Plugin System"),
    ("extension", "Extensions"),
    ("cli", "CLI"),
    ("cmd", "CLI / Commands"),
    ("command", "CLI / Commands"),
    ("test", "Tests"),
    ("spec", "Tests"),
    ("ui", "UI / Frontend"),
    ("view", "UI / Frontend"),
    ("component", "UI Components"),
    ("page", "Pages"),
    ("style", "Styles / CSS"),
    ("template", "Templates"),
    ("script", "Scripts"),
    ("tool", "Tools"),
    ("deploy", "Deployment"),
    ("docker", "Docker / Infra"),
    ("infra", "Infrastructure"),
    ("ci", "CI/CD"),
    ("log", "Logging / Observability"),
    ("metric", "Metrics / Monitoring"),
    ("eval", "Evaluation"),
    ("assertion", "Assertions / Graders"),
    ("grader", "Assertions / Graders"),
    ("prompt", "Prompt Management"),
    ("transform", "Data Transform"),
    ("parse", "Parsing"),
    ("serial", "Serialization"),
    ("format", "Formatting"),
    ("export", "Export / Output"),
    ("import", "Import / Input"),
    ("migrate", "Migration"),
    ("schema", "Schema / Types"),
    ("type", "Schema / Types"),
    ("error", "Error Handling"),
    ("exception", "Error Handling"),
]


def _categorize_dir(dir_name: str) -> str:
    """Map a directory name to a human-readable category label."""
    lower = dir_name.lower()
    for keyword, label in _CATEGORY_RULES:
        if keyword in lower:
            return label
    return ""


def _add_category_labels(node: dict) -> None:
    """Walk treemap tree and add 'category' to directory nodes."""
    if "children" not in node:
        return
    for child in node["children"]:
        if "children" in child:
            cat = _categorize_dir(child["name"])
            if cat:
                child["category"] = cat
            _add_category_labels(child)



def build_treemap_json(
    results_path: str,
    confirmed_bugs: dict[str, list[dict]] | None = None,
) -> str:
    """Build treemap JSON string from stress-test results.

    Returns JSON string suitable for passing to generate_dashboard(treemap_json=...).
    """
    data = json.loads(Path(results_path).read_text(encoding="utf-8"))
    results = data.get("results", [])
    metadata = data.get("metadata", {})
    n_modifications = metadata.get("n_modifications", len(results))
    repo_name = metadata.get("repo_name", "repository")

    file_risks = aggregate_results(results, n_modifications, confirmed_bugs)
    risky_files = {k: v for k, v in file_risks.items() if v.risk_score > 0}
    treemap_data = build_treemap_data(risky_files, repo_name)
    _add_category_labels(treemap_data)

    return json.dumps(treemap_data, ensure_ascii=False)


def generate_heatmap(
    results_path: str,
    output_path: str,
    confirmed_bugs: dict[str, list[dict]] | None = None,
) -> str:
    """Generate unified dashboard with landmine map from stress-test results.

    The output_path is kept for backward compatibility but the actual output
    goes to the unified dashboard at .delta-lint/findings/dashboard.html.
    A symlink or copy is placed at output_path for callers that expect it there.

    Returns:
        Path to the generated HTML file
    """
    # Build treemap JSON
    treemap = build_treemap_json(results_path, confirmed_bugs)

    # Determine repo base path from output_path
    # output_path is typically .delta-lint/stress-test/landmine_map.html
    out = Path(output_path)
    # Walk up to find .delta-lint parent
    base_path = out.parent.parent.parent  # repo root (above .delta-lint/)
    if not (base_path / ".delta-lint").exists():
        # Fallback: try output_path's grandparent
        base_path = out.parent.parent

    from findings import generate_dashboard
    dash_path = generate_dashboard(base_path, treemap_json=treemap)

    print(f"Dashboard generated: {dash_path}", file=sys.stderr)
    return str(dash_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate HTML heatmap from delta-lint stress-test results"
    )
    parser.add_argument("--input", required=True, help="Path to results.json")
    parser.add_argument("--output", default="landmine_map.html", help="Output HTML path")
    parser.add_argument("--bugs", default=None, help="Path to confirmed_bugs.json (optional)")

    args = parser.parse_args()

    confirmed = None
    if args.bugs:
        confirmed = json.loads(Path(args.bugs).read_text(encoding="utf-8"))

    generate_heatmap(
        results_path=args.input,
        output_path=args.output,
        confirmed_bugs=confirmed,
    )
