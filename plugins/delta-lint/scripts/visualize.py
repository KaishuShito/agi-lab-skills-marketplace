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
from datetime import datetime
from pathlib import Path
from string import Template

from aggregate import aggregate_results, build_treemap_data, FileRisk


_TEMPLATE_DIR = Path(__file__).parent / "templates"


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


def _load_template(name: str = "dashboard.html") -> Template:
    """Load an HTML template from the templates directory."""
    path = _TEMPLATE_DIR / name
    return Template(path.read_text(encoding="utf-8"))


def generate_heatmap(
    results_path: str,
    output_path: str,
    confirmed_bugs: dict[str, list[dict]] | None = None,
) -> str:
    """Generate HTML heatmap from stress-test results.

    Args:
        results_path: Path to results.json from stress_test.py
        output_path: Path to write the HTML file
        confirmed_bugs: Optional map of file_path -> [{issue, repo}]

    Returns:
        Path to the generated HTML file
    """
    # Load results
    data = json.loads(Path(results_path).read_text(encoding="utf-8"))
    metadata = data.get("metadata", {})
    results = data.get("results", [])
    n_modifications = metadata.get("n_modifications", len(results))
    repo_name = metadata.get("repo_name", "repository")
    timestamp = metadata.get("timestamp", datetime.now().strftime("%Y-%m-%d"))

    # Aggregate
    file_risks = aggregate_results(results, n_modifications, confirmed_bugs)

    # Only keep files with risk > 0 for treemap (skip safe files)
    risky_files = {k: v for k, v in file_risks.items() if v.risk_score > 0}

    # Build treemap data (risky files only)
    treemap_data = build_treemap_data(risky_files, repo_name)

    # Add human-readable category labels to directory groups
    _add_category_labels(treemap_data)

    # Stats for header
    total_findings = sum(len(r.get("findings", [])) for r in results)
    hit_mods = sum(1 for r in results if r.get("findings"))
    files_at_risk = sum(1 for r in file_risks.values() if r.risk_score > 0)
    high_risk_files = sum(1 for r in file_risks.values() if r.risk_score > 0.35)

    # Render template
    template = _load_template()
    html = template.safe_substitute(
        repo_name=repo_name,
        timestamp=timestamp,
        n_modifications=n_modifications,
        total_findings=total_findings,
        hit_rate=f"{hit_mods}/{n_modifications}",
        files_at_risk=files_at_risk,
        high_risk_files=high_risk_files,
        treemap_json=json.dumps(treemap_data, ensure_ascii=False),
    )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    print(f"Heatmap generated: {out}", file=sys.stderr)
    return str(out)


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
