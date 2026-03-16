"""
Visualization layer for delta-lint stress-test.

Generates a single self-contained HTML file with a D3.js treemap heatmap
showing per-file risk scores from stress-test results.

No Python dependencies beyond stdlib. Output HTML uses D3.js via CDN.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from aggregate import aggregate_results, build_treemap_data, FileRisk


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

    # Add all repo files as risk=0 for full treemap coverage
    repo_path = metadata.get("repo", "")
    if repo_path and Path(repo_path).is_dir():
        try:
            from retrieval import filter_source_files
            result = subprocess.run(
                ["git", "ls-files"],
                capture_output=True, text=True, cwd=repo_path, timeout=10,
            )
            all_files = filter_source_files(result.stdout.strip().split("\n"))
            for fpath in all_files:
                if fpath not in file_risks:
                    full = Path(repo_path) / fpath
                    lines = 0
                    if full.exists():
                        try:
                            lines = len(full.read_text(encoding="utf-8", errors="ignore").split("\n"))
                        except Exception:
                            pass
                    file_risks[fpath] = FileRisk(path=fpath, lines=lines)
        except Exception:
            pass  # Fall back to risk-only files if git ls-files fails

    # Build treemap data
    treemap_data = build_treemap_data(file_risks, repo_name)

    # Stats for header
    total_findings = sum(len(r.get("findings", [])) for r in results)
    hit_mods = sum(1 for r in results if r.get("findings"))
    files_at_risk = sum(1 for r in file_risks.values() if r.risk_score > 0)
    high_risk_files = sum(1 for r in file_risks.values() if r.risk_score > 0.35)

    # Generate HTML
    html = _build_html(
        treemap_data=treemap_data,
        repo_name=repo_name,
        timestamp=timestamp,
        n_modifications=n_modifications,
        total_findings=total_findings,
        hit_mods=hit_mods,
        files_at_risk=files_at_risk,
        high_risk_files=high_risk_files,
    )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    print(f"Heatmap generated: {out}", file=sys.stderr)
    return str(out)


def _build_html(
    treemap_data: dict,
    repo_name: str,
    timestamp: str,
    n_modifications: int,
    total_findings: int,
    hit_mods: int,
    files_at_risk: int,
    high_risk_files: int,
) -> str:
    """Build the complete HTML string."""
    treemap_json = json.dumps(treemap_data, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>delta-lint Landmine Map: {repo_name}</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #0d1117;
  color: #c9d1d9;
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}}

/* Header */
.header {{
  padding: 16px 24px;
  background: #161b22;
  border-bottom: 1px solid #30363d;
  display: flex;
  align-items: center;
  gap: 24px;
  flex-shrink: 0;
}}
.header h1 {{
  font-size: 18px;
  font-weight: 600;
  color: #f0f6fc;
}}
.header h1 span {{
  color: #7ee787;
  font-weight: 400;
}}
.stats {{
  display: flex;
  gap: 16px;
  font-size: 13px;
}}
.stat {{
  padding: 4px 10px;
  background: #21262d;
  border-radius: 6px;
  border: 1px solid #30363d;
}}
.stat .value {{
  font-weight: 600;
  color: #f0f6fc;
}}
.stat.danger .value {{ color: #f85149; }}
.stat.warning .value {{ color: #d29922; }}
.stat.success .value {{ color: #7ee787; }}

/* Main layout */
.main {{
  display: flex;
  flex: 1;
  overflow: hidden;
}}

/* Treemap */
#treemap {{
  flex: 1;
  position: relative;
  overflow: hidden;
}}
.cell {{
  position: absolute;
  overflow: hidden;
  border: 1px solid #0d1117;
  cursor: pointer;
  transition: opacity 0.15s;
}}
.cell:hover {{
  opacity: 0.85;
  z-index: 10;
}}
.cell-label {{
  padding: 3px 5px;
  font-size: 11px;
  color: rgba(255,255,255,0.9);
  text-shadow: 0 1px 2px rgba(0,0,0,0.8);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  pointer-events: none;
}}
.cell-hits {{
  padding: 0 5px;
  font-size: 10px;
  color: rgba(255,255,255,0.7);
  pointer-events: none;
}}
.cell-bug {{
  position: absolute;
  top: 2px;
  right: 4px;
  font-size: 14px;
  pointer-events: none;
}}

/* Tooltip */
.tooltip {{
  position: fixed;
  padding: 10px 14px;
  background: #1c2128;
  border: 1px solid #444c56;
  border-radius: 8px;
  font-size: 12px;
  pointer-events: none;
  z-index: 1000;
  max-width: 360px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.4);
  display: none;
}}
.tooltip .tt-path {{
  font-weight: 600;
  color: #f0f6fc;
  margin-bottom: 6px;
  word-break: break-all;
}}
.tooltip .tt-row {{
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin: 2px 0;
}}
.tooltip .tt-label {{ color: #8b949e; }}
.tooltip .tt-value {{ color: #c9d1d9; font-weight: 500; }}

/* Sidebar */
.sidebar {{
  width: 340px;
  background: #161b22;
  border-left: 1px solid #30363d;
  overflow-y: auto;
  padding: 16px;
  flex-shrink: 0;
  display: none;
}}
.sidebar.active {{ display: block; }}
.sidebar h2 {{
  font-size: 14px;
  color: #f0f6fc;
  margin-bottom: 12px;
  word-break: break-all;
}}
.sidebar .close-btn {{
  float: right;
  background: none;
  border: none;
  color: #8b949e;
  cursor: pointer;
  font-size: 18px;
}}
.finding-card {{
  background: #21262d;
  border: 1px solid #30363d;
  border-radius: 6px;
  padding: 10px;
  margin-bottom: 8px;
  font-size: 12px;
}}
.finding-card .fc-pattern {{
  font-weight: 600;
  color: #d29922;
  margin-bottom: 4px;
}}
.finding-card .fc-severity {{
  display: inline-block;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 11px;
  font-weight: 600;
}}
.fc-severity.high {{ background: #f8514922; color: #f85149; }}
.fc-severity.medium {{ background: #d2992222; color: #d29922; }}
.fc-severity.low {{ background: #7ee78722; color: #7ee787; }}
.finding-card .fc-desc {{
  margin-top: 6px;
  color: #8b949e;
  line-height: 1.4;
}}
.finding-card .fc-mod {{
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px solid #30363d;
  color: #8b949e;
  font-style: italic;
}}
.bug-tag {{
  display: inline-block;
  padding: 2px 8px;
  background: #f8514933;
  color: #f85149;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  margin-bottom: 8px;
}}

/* Legend */
.legend {{
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 24px;
  background: #161b22;
  border-top: 1px solid #30363d;
  font-size: 12px;
  flex-shrink: 0;
}}
.legend-item {{
  display: flex;
  align-items: center;
  gap: 4px;
}}
.legend-color {{
  width: 14px;
  height: 14px;
  border-radius: 3px;
  border: 1px solid #30363d;
}}
.legend .brand {{
  margin-left: auto;
  color: #484f58;
  font-size: 11px;
}}
</style>
</head>
<body>

<div class="header">
  <h1>delta-lint <span>Landmine Map</span></h1>
  <div class="stats">
    <div class="stat">
      <span class="tt-label">Repo:</span>
      <span class="value">{repo_name}</span>
    </div>
    <div class="stat">
      <span class="tt-label">Modifications:</span>
      <span class="value">{n_modifications}</span>
    </div>
    <div class="stat warning">
      <span class="tt-label">Findings:</span>
      <span class="value">{total_findings}</span>
    </div>
    <div class="stat danger">
      <span class="tt-label">High-risk files:</span>
      <span class="value">{high_risk_files}</span>
    </div>
    <div class="stat success">
      <span class="tt-label">Hit rate:</span>
      <span class="value">{hit_mods}/{n_modifications}</span>
    </div>
  </div>
</div>

<div class="main">
  <div id="treemap"></div>
  <div class="sidebar" id="sidebar">
    <button class="close-btn" onclick="closeSidebar()">&times;</button>
    <div id="sidebar-content"></div>
  </div>
</div>

<div class="tooltip" id="tooltip"></div>

<div class="legend">
  <div class="legend-item"><div class="legend-color" style="background:#2d5016"></div> Safe (0 hits)</div>
  <div class="legend-item"><div class="legend-color" style="background:#4a8c1c"></div> Low risk</div>
  <div class="legend-item"><div class="legend-color" style="background:#f0ad4e"></div> Medium risk</div>
  <div class="legend-item"><div class="legend-color" style="background:#e67e22"></div> High risk</div>
  <div class="legend-item"><div class="legend-color" style="background:#c0392b"></div> Landmine</div>
  <span>&#11088; = Confirmed bug</span>
  <span class="brand">delta-lint stress-test &middot; {timestamp}</span>
</div>

<script>
const data = {treemap_json};

// Color scale — relative to max risk in this dataset
const maxRisk = Math.max(...data.children.flatMap(function flatLeaves(node) {{
  return node.children ? node.children.flatMap(flatLeaves) : [node.risk_score || 0];
}}), 0.01);

function riskColor(score) {{
  if (score <= 0) return '#2d5016';
  const ratio = score / maxRisk;
  if (ratio <= 0.25) return '#4a8c1c';
  if (ratio <= 0.50) return '#f0ad4e';
  if (ratio <= 0.75) return '#e67e22';
  return '#c0392b';
}}

// Treemap layout
const container = document.getElementById('treemap');
const width = container.clientWidth;
const height = container.clientHeight;

const root = d3.hierarchy(data)
  .sum(d => {{
    const risk = d.risk_score || 0;
    // Risk-based sizing: dangerous files get bigger tiles
    // risk=0 → 1 (tiny), risk=0.1 → 50, risk=0.5 → 500, risk=1.0 → 1000
    return risk > 0 ? Math.max(50, Math.round(risk * 1000)) : 1;
  }})
  .sort((a, b) => (b.value || 0) - (a.value || 0));

d3.treemap()
  .size([width, height])
  .padding(2)
  .paddingTop(18)
  .round(true)(root);

// Render group labels
root.children && root.children.forEach(group => {{
  const label = document.createElement('div');
  label.style.cssText = `
    position: absolute;
    left: ${{group.x0}}px; top: ${{group.y0}}px;
    width: ${{group.x1 - group.x0}}px; height: 16px;
    font-size: 11px; font-weight: 600; color: #8b949e;
    padding: 1px 4px; overflow: hidden; white-space: nowrap;
    text-overflow: ellipsis; pointer-events: none;
  `;
  label.textContent = group.data.name;
  container.appendChild(label);
}});

// Render leaf cells
const leaves = root.leaves();
leaves.forEach(leaf => {{
  const d = leaf.data;
  const w = leaf.x1 - leaf.x0;
  const h = leaf.y1 - leaf.y0;
  if (w < 3 || h < 3) return; // too small

  const cell = document.createElement('div');
  cell.className = 'cell';
  cell.style.cssText = `
    left: ${{leaf.x0}}px; top: ${{leaf.y0}}px;
    width: ${{w}}px; height: ${{h}}px;
    background: ${{riskColor(d.risk_score || 0)}};
  `;

  // Label
  if (w > 40 && h > 20) {{
    const label = document.createElement('div');
    label.className = 'cell-label';
    label.textContent = d.name;
    cell.appendChild(label);
  }}

  // Hit count
  if (w > 40 && h > 34 && (d.hit_count || 0) > 0) {{
    const hits = document.createElement('div');
    hits.className = 'cell-hits';
    hits.textContent = `${{d.hit_count}} hit${{d.hit_count > 1 ? 's' : ''}}`;
    cell.appendChild(hits);
  }}

  // Confirmed bug star
  if (d.confirmed_bugs && d.confirmed_bugs.length > 0) {{
    const star = document.createElement('div');
    star.className = 'cell-bug';
    star.textContent = '\\u2B50';
    cell.appendChild(star);
  }}

  // Tooltip
  const tooltip = document.getElementById('tooltip');
  cell.addEventListener('mouseenter', e => {{
    tooltip.innerHTML = `
      <div class="tt-path">${{d.full_path || d.name}}</div>
      <div class="tt-row"><span class="tt-label">Risk score</span><span class="tt-value">${{(d.risk_score || 0).toFixed(3)}}</span></div>
      <div class="tt-row"><span class="tt-label">Hit count</span><span class="tt-value">${{d.hit_count || 0}}</span></div>
      <div class="tt-row"><span class="tt-label">Max severity</span><span class="tt-value">${{d.max_severity || 'none'}}</span></div>
      <div class="tt-row"><span class="tt-label">Patterns</span><span class="tt-value">${{(d.patterns || []).join(', ') || 'none'}}</span></div>
      <div class="tt-row"><span class="tt-label">Findings</span><span class="tt-value">${{d.findings_count || 0}}</span></div>
      ${{d.confirmed_bugs && d.confirmed_bugs.length > 0 ?
        `<div class="tt-row"><span class="tt-label">\\u2B50 Confirmed</span><span class="tt-value">${{d.confirmed_bugs.map(b => b.issue).join(', ')}}</span></div>` : ''}}
    `;
    tooltip.style.display = 'block';
  }});
  cell.addEventListener('mousemove', e => {{
    tooltip.style.left = (e.clientX + 12) + 'px';
    tooltip.style.top = (e.clientY + 12) + 'px';
  }});
  cell.addEventListener('mouseleave', () => {{
    tooltip.style.display = 'none';
  }});

  // Click → sidebar
  cell.addEventListener('click', () => {{
    showSidebar(d);
  }});

  container.appendChild(cell);
}});

// Sidebar
function showSidebar(d) {{
  const sb = document.getElementById('sidebar');
  const content = document.getElementById('sidebar-content');
  sb.classList.add('active');

  let html = `<h2>${{d.full_path || d.name}}</h2>`;

  if (d.confirmed_bugs && d.confirmed_bugs.length > 0) {{
    d.confirmed_bugs.forEach(b => {{
      html += `<div class="bug-tag">\\u2B50 Confirmed: ${{b.issue}} (${{b.repo || ''}})</div>`;
    }});
  }}

  html += `
    <div style="margin-bottom:12px;font-size:12px;color:#8b949e">
      Risk: ${{(d.risk_score || 0).toFixed(3)}} &middot;
      Hits: ${{d.hit_count || 0}} &middot;
      Severity: ${{d.max_severity || 'none'}}
    </div>
  `;

  const findings = d.findings_sample || [];
  if (findings.length === 0) {{
    html += '<p style="color:#8b949e">No findings for this file.</p>';
  }} else {{
    findings.forEach(f => {{
      const sev = f.severity || 'low';
      html += `
        <div class="finding-card">
          <div class="fc-pattern">${{f.pattern || 'Unknown pattern'}}</div>
          <span class="fc-severity ${{sev}}">${{sev}}</span>
          <div class="fc-desc">${{f.description || f.summary || ''}}</div>
          <div class="fc-mod">Triggered by: ${{f.modification_desc || '?'}}</div>
        </div>
      `;
    }});
    if ((d.findings_count || 0) > findings.length) {{
      html += `<p style="color:#8b949e;font-size:11px;margin-top:8px">
        Showing ${{findings.length}} of ${{d.findings_count}} findings.
      </p>`;
    }}
  }}

  content.innerHTML = html;
}}

function closeSidebar() {{
  document.getElementById('sidebar').classList.remove('active');
}}

// Resize handler
window.addEventListener('resize', () => location.reload());
</script>
</body>
</html>"""


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
