"""
src/reports/reconciliation_report.py
-------------------------------------
Generates a formatted HTML reconciliation report using Jinja2.

The report is self-contained HTML (no external CDN dependencies) and
includes:
  - Summary banner: ledger name, period, run timestamp
  - KPI cards: journals analyzed, imbalances detected, corrections drafted
  - Per-journal detail table with root cause and corrective action
  - Status badges: RESOLVED (green), PENDING_APPROVAL (amber), REQUIRES_INVESTIGATION (red)
  - Appendix: list of generated FBDI files

The Jinja2 template is embedded in this module as a string constant
so the package has no external template file dependency.  An external
templates/recon_report.html.j2 file is also written for customization.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jinja2 import Environment, BaseLoader, select_autoescape

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Embedded Jinja2 template
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>GL Reconciliation Report — {{ period_name }}</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Segoe UI', Arial, sans-serif;
      font-size: 13px;
      background: #f5f6fa;
      color: #1a1a2e;
    }
    header {
      background: linear-gradient(135deg, #c0392b 0%, #922b21 100%);
      color: #fff;
      padding: 28px 40px;
    }
    header h1 { font-size: 22px; font-weight: 700; margin-bottom: 6px; }
    header .meta { font-size: 12px; opacity: 0.85; }
    .container { max-width: 1200px; margin: 0 auto; padding: 32px 24px; }

    /* KPI cards */
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 16px;
      margin-bottom: 32px;
    }
    .kpi-card {
      background: #fff;
      border-radius: 8px;
      padding: 20px 24px;
      box-shadow: 0 2px 8px rgba(0,0,0,.08);
      border-left: 4px solid #c0392b;
    }
    .kpi-card .value { font-size: 32px; font-weight: 700; color: #c0392b; }
    .kpi-card .label { font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: .5px; margin-top: 4px; }

    /* Section headings */
    h2 { font-size: 16px; font-weight: 600; margin-bottom: 16px; color: #1a1a2e; border-bottom: 2px solid #e8e8e8; padding-bottom: 8px; }

    /* Status badges */
    .badge {
      display: inline-block;
      padding: 3px 10px;
      border-radius: 12px;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: .4px;
    }
    .badge-resolved { background: #d5f5e3; color: #1d7a45; }
    .badge-pending  { background: #fef9e7; color: #b7770d; }
    .badge-investigate { background: #fdedec; color: #c0392b; }

    /* Table */
    .findings-table {
      width: 100%;
      border-collapse: collapse;
      background: #fff;
      border-radius: 8px;
      overflow: hidden;
      box-shadow: 0 2px 8px rgba(0,0,0,.06);
      margin-bottom: 32px;
    }
    .findings-table thead {
      background: #1a1a2e;
      color: #fff;
    }
    .findings-table th {
      padding: 12px 14px;
      text-align: left;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .5px;
    }
    .findings-table td {
      padding: 11px 14px;
      border-bottom: 1px solid #f0f0f0;
      vertical-align: top;
      font-size: 12px;
    }
    .findings-table tr:last-child td { border-bottom: none; }
    .findings-table tr:hover td { background: #fafafa; }
    .imbalance { font-weight: 700; color: #c0392b; }
    .mono { font-family: 'Courier New', monospace; font-size: 11px; }

    /* FBDI appendix */
    .fbdi-list {
      background: #fff;
      border-radius: 8px;
      padding: 16px 20px;
      box-shadow: 0 2px 8px rgba(0,0,0,.06);
    }
    .fbdi-list li { font-family: monospace; font-size: 12px; padding: 4px 0; color: #333; }

    /* Summary box */
    .agent-summary {
      background: #fff;
      border-radius: 8px;
      padding: 20px 24px;
      box-shadow: 0 2px 8px rgba(0,0,0,.06);
      margin-bottom: 32px;
      white-space: pre-wrap;
      font-size: 12px;
      line-height: 1.6;
      border-left: 4px solid #2980b9;
    }

    footer {
      text-align: center;
      font-size: 11px;
      color: #aaa;
      padding: 24px 0 40px;
    }
  </style>
</head>
<body>
<header>
  <h1>Oracle GL Reconciliation Report</h1>
  <div class="meta">
    Ledger: <strong>{{ ledger_name }}</strong> &nbsp;|&nbsp;
    Period: <strong>{{ period_name }}</strong> &nbsp;|&nbsp;
    Generated: {{ generated_at }} UTC &nbsp;|&nbsp;
    Overall Status:
    {% if overall_status == 'RESOLVED' %}
      <span class="badge badge-resolved">{{ overall_status }}</span>
    {% elif overall_status == 'PENDING_APPROVAL' %}
      <span class="badge badge-pending">{{ overall_status }}</span>
    {% else %}
      <span class="badge badge-investigate">{{ overall_status }}</span>
    {% endif %}
  </div>
</header>

<div class="container">

  <!-- KPI Summary Cards -->
  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="value">{{ journals_analyzed }}</div>
      <div class="label">Journals Analyzed</div>
    </div>
    <div class="kpi-card">
      <div class="value">{{ imbalances_found }}</div>
      <div class="label">Imbalances Detected</div>
    </div>
    <div class="kpi-card">
      <div class="value">{{ corrections_drafted }}</div>
      <div class="label">Corrections Drafted</div>
    </div>
    <div class="kpi-card">
      <div class="value">{{ pending_approval }}</div>
      <div class="label">Pending Approval</div>
    </div>
    <div class="kpi-card">
      <div class="value">{{ requires_investigation }}</div>
      <div class="label">Need Investigation</div>
    </div>
  </div>

  <!-- Findings Detail Table -->
  <h2>Journal Findings</h2>
  {% if findings %}
  <table class="findings-table">
    <thead>
      <tr>
        <th>JE Header ID</th>
        <th>Journal Name</th>
        <th>Imbalance</th>
        <th>Root Cause</th>
        <th>Corrective Action</th>
        <th>FBDI File</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>
    {% for f in findings %}
      <tr>
        <td class="mono">{{ f.journal_header_id }}</td>
        <td>{{ f.journal_name | default('—') }}</td>
        <td class="imbalance">{{ '%.2f' % f.imbalance_amount | float }}</td>
        <td>{{ f.root_cause | default('—') }}</td>
        <td>{{ f.corrective_action | default('—') }}</td>
        <td class="mono">
          {% if f.fbdi_file %}
            {{ f.fbdi_file | basename }}
          {% else %}—{% endif %}
        </td>
        <td>
          {% if f.status == 'RESOLVED' %}
            <span class="badge badge-resolved">{{ f.status }}</span>
          {% elif f.status == 'PENDING_APPROVAL' %}
            <span class="badge badge-pending">{{ f.status }}</span>
          {% else %}
            <span class="badge badge-investigate">{{ f.status }}</span>
          {% endif %}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p style="color:#666;margin-bottom:32px;">No imbalances found for {{ period_name }}. GL is balanced.</p>
  {% endif %}

  <!-- Agent Summary -->
  {% if agent_summary %}
  <h2>Agent Reasoning Summary</h2>
  <div class="agent-summary">{{ agent_summary }}</div>
  {% endif %}

  <!-- FBDI Files Appendix -->
  {% if fbdi_files %}
  <h2>Generated FBDI Files</h2>
  <div class="fbdi-list">
    <ul>
    {% for f in fbdi_files %}
      <li>{{ f }}</li>
    {% endfor %}
    </ul>
  </div>
  {% endif %}

</div>

<footer>
  Generated by oracle-gl-reconciliation-agent &nbsp;|&nbsp;
  Oracle Fusion Cloud GL Reconciliation &nbsp;|&nbsp;
  {{ generated_at }}
</footer>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_report(
    findings: list[dict],
    period_name: str,
    ledger_name: str,
    output_dir: str = "output",
    agent_summary: str = "",
    fbdi_files: Optional[list[str]] = None,
    journals_analyzed: int = 0,
) -> Path:
    """
    Render and write the HTML reconciliation report.

    Parameters
    ----------
    findings : list[dict]
        Per-journal finding dicts from the agent.  Each should have:
          journal_header_id, journal_name, imbalance_amount,
          root_cause, corrective_action, fbdi_file, status
    period_name : str
    ledger_name : str
    output_dir : str
    agent_summary : str
        Final plain-text LLM summary (rendered as pre-formatted text).
    fbdi_files : list[str], optional
        Paths to generated FBDI files for the appendix.
    journals_analyzed : int
        Total journals inspected (from agent run state).

    Returns
    -------
    Path
        Absolute path to the written HTML file.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    safe_period = period_name.replace("-", "").replace("/", "")
    file_name = f"recon_report_{safe_period}.html"
    file_path = out_path / file_name

    # Compute summary counts
    imbalances_found = len(findings)
    corrections_drafted = sum(1 for f in findings if f.get("fbdi_file"))
    pending_approval = sum(1 for f in findings if f.get("status") == "PENDING_APPROVAL")
    requires_investigation = sum(
        1 for f in findings if f.get("status") == "REQUIRES_INVESTIGATION"
    )
    resolved = sum(1 for f in findings if f.get("status") == "RESOLVED")

    if imbalances_found == 0 or imbalances_found == resolved:
        overall_status = "RESOLVED"
    elif requires_investigation > 0:
        overall_status = "REQUIRES_INVESTIGATION"
    else:
        overall_status = "PENDING_APPROVAL"

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Jinja2 environment — no file loader needed; template is embedded
    env = Environment(
        loader=BaseLoader(),
        autoescape=select_autoescape(["html"]),
    )

    # Custom filter: basename of a file path
    env.filters["basename"] = lambda p: Path(p).name if p else ""

    template = env.from_string(_HTML_TEMPLATE)
    rendered = template.render(
        period_name=period_name,
        ledger_name=ledger_name,
        generated_at=generated_at,
        overall_status=overall_status,
        journals_analyzed=journals_analyzed or imbalances_found,
        imbalances_found=imbalances_found,
        corrections_drafted=corrections_drafted,
        pending_approval=pending_approval,
        requires_investigation=requires_investigation,
        findings=findings,
        agent_summary=agent_summary,
        fbdi_files=fbdi_files or [],
    )

    file_path.write_text(rendered, encoding="utf-8")
    logger.info("Reconciliation report written: %s", file_path.resolve())

    # Also write the Jinja2 template file for user customization
    _write_template_file(out_path.parent)

    return file_path.resolve()


def _write_template_file(project_root: Path) -> None:
    """Write the embedded template to templates/ for user customization."""
    templates_dir = project_root / "templates"
    templates_dir.mkdir(exist_ok=True)
    template_file = templates_dir / "recon_report.html.j2"
    if not template_file.exists():
        template_file.write_text(_HTML_TEMPLATE, encoding="utf-8")
        logger.debug("Jinja2 template written: %s", template_file)
