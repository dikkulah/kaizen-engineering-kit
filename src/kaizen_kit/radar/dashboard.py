"""Zero-CDN self-contained interactive HTML Graph & Hotspot Dashboard generator."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

import duckdb

_DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>__TITLE__</title>
<style>
  :root {
    --bg: #0b1020; --panel: #131b31; --panel-2: #18223d; --border: #273558;
    --text: #e6ecf8; --muted: #93a4c8; --accent: #38bdf8; --warn: #f59e0b;
    --danger: #ef4444; --ok: #10b981; --rust: #fb923c; --java: #60a5fa; --go: #2dd4bf;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.45; }
  header { padding: 16px 24px; border-bottom: 1px solid var(--border); background: #0e1529;
           display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px; }
  header h1 { margin: 0; font-size: 20px; font-weight: 700; letter-spacing: 0.2px; }
  .badge { display: inline-block; padding: 3px 9px; border-radius: 999px; font-size: 12px;
           background: var(--panel-2); border: 1px solid var(--border); color: var(--muted); }
  .container { max-width: 1520px; margin: 0 auto; padding: 18px 24px; display: grid; gap: 18px; }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
  .kpi { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }
  .kpi .label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .kpi .value { font-size: 24px; font-weight: 700; margin-top: 4px; color: var(--accent); }
  .grid-2 { display: grid; grid-template-columns: 1.4fr 1fr; gap: 18px; }
  @media (max-width: 1100px) { .grid-2 { grid-template-columns: 1fr; } }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
  .card-hdr { padding: 12px 16px; border-bottom: 1px solid var(--border); display: flex;
              justify-content: space-between; align-items: center; gap: 8px; background: var(--panel-2); }
  .card-hdr h2 { margin: 0; font-size: 14px; font-weight: 600; }
  .card-body { padding: 14px 16px; }
  #graph-canvas { width: 100%; height: 470px; display: block; background: #080d1a; border-radius: 6px; cursor: grab; }
  .controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  input, select { background: #0b1020; color: var(--text); border: 1px solid var(--border);
                  border-radius: 6px; padding: 6px 10px; font-size: 13px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; cursor: pointer; user-select: none; position: sticky; top: 0; background: var(--panel-2); }
  tr:hover td { background: rgba(56, 189, 248, 0.08); }
  .table-wrap { max-height: 430px; overflow: auto; }
  .pill { padding: 2px 7px; border-radius: 5px; font-size: 11px; font-weight: 600; }
  .lang-rust { background: rgba(251, 146, 60, 0.16); color: var(--rust); }
  .lang-java { background: rgba(96, 165, 250, 0.16); color: var(--java); }
  .lang-go { background: rgba(45, 212, 191, 0.16); color: var(--go); }
  .sev-error { background: rgba(239, 68, 68, 0.2); color: #fca5a5; }
  .sev-warning { background: rgba(245, 158, 11, 0.2); color: #fcd34d; }
  .sev-info { background: rgba(56, 189, 248, 0.18); color: #7dd3fc; }
  pre { background: #080d1a; border: 1px solid var(--border); border-radius: 8px; padding: 12px;
        overflow: auto; font-size: 12px; max-height: 360px; white-space: pre-wrap; }
  .legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; color: var(--muted); margin-top: 8px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 4px; }
  .drawer { max-height: 470px; overflow: auto; }
  .hotspot-grid { display: flex; flex-wrap: wrap; gap: 6px; }
  .hs-tile { border-radius: 6px; padding: 8px; font-size: 12px; border: 1px solid rgba(255,255,255,0.08);
             cursor: pointer; min-width: 130px; flex: 1 1 160px; }
</style>
</head>
<body>
<header>
  <div>
    <h1>__TITLE__</h1>
    <div style="font-size:12px;color:var(--muted);margin-top:4px;">__SUBTITLE__</div>
  </div>
  <div class="controls">
    <span class="badge">Refactor Radar v1.0 (Zero-CDN Offline)</span>
  </div>
</header>
<div class="container">
  <div class="kpis" id="kpis"></div>
  <div class="grid-2">
    <div class="card">
      <div class="card-hdr">
        <h2>Interactive Call, Semantic Clone &amp; Boundary Graph</h2>
        <div class="controls">
          <select id="edge-filter">
            <option value="ALL">All Edges</option>
            <option value="CALLS">SCIP CALLS</option>
            <option value="CLONE">Semantic Clones</option>
            <option value="CO_CHANGES_WITH">Git Co-Changes</option>
            <option value="BOUNDARY">Boundaries</option>
          </select>
        </div>
      </div>
      <div class="card-body">
        <canvas id="graph-canvas"></canvas>
        <div class="legend">
          <span><span class="dot" style="background:var(--rust)"></span>Rust</span>
          <span><span class="dot" style="background:var(--java)"></span>Java</span>
          <span><span class="dot" style="background:var(--go)"></span>Go</span>
          <span><span class="dot" style="background:var(--warn)"></span>Boundary Contract</span>
          <span><span class="dot" style="background:var(--danger)"></span>Red Halo = Smells</span>
          <span>Dashed Cyan = Semantic Clone</span>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-hdr"><h2>Inspector &amp; Hotspot Heatmap</h2></div>
      <div class="card-body drawer" id="inspector">
        <div style="color:var(--muted);font-size:13px;margin-bottom:10px;">
          Click any graph node, hotspot tile, or table row to inspect code &amp; smells.
        </div>
        <div class="hotspot-grid" id="hotspot-grid"></div>
        <div id="node-detail" style="margin-top:14px;"></div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-hdr">
      <h2>Top ROI Refactor Candidates</h2>
      <div class="controls">
        <input id="search-box" type="search" placeholder="Filter symbol or file..." />
        <select id="lang-filter">
          <option value="">All Languages</option>
          <option value="rust">Rust</option>
          <option value="java">Java</option>
          <option value="go">Go</option>
        </select>
      </div>
    </div>
    <div class="card-body table-wrap">
      <table id="roi-table">
        <thead>
          <tr>
            <th data-col="roi_score">ROI</th>
            <th data-col="service_name">Service</th>
            <th data-col="language">Lang</th>
            <th data-col="name">Symbol</th>
            <th data-col="file_path">Location</th>
            <th data-col="complexity">Complexity</th>
            <th data-col="churn_90d">Churn 90d</th>
            <th data-col="fan_in">Fan-In</th>
            <th data-col="smell_count">Smells</th>
            <th data-col="clone_count">Clones</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <div class="card-hdr"><h2>Semantic Clones &amp; Structural Smells / Contracts</h2></div>
      <div class="card-body table-wrap" id="clones-smells"></div>
    </div>
    <div class="card">
      <div class="card-hdr"><h2>Synthesized Refactor Action Plan</h2></div>
      <div class="card-body"><pre id="report-md"></pre></div>
    </div>
  </div>
</div>

<script id="radar-data" type="application/json">__JSON_PAYLOAD__</script>
<script>
(function() {
  const DATA = JSON.parse(document.getElementById("radar-data").textContent);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

  // KPIs
  const kpiBox = document.getElementById("kpis");
  for (const k of DATA.kpis || []) {
    const d = document.createElement("div");
    d.className = "kpi";
    d.innerHTML = `<div class="label">${esc(k.label)}</div><div class="value">${esc(k.value)}</div>`;
    kpiBox.appendChild(d);
  }

  // Report markdown
  document.getElementById("report-md").textContent = DATA.report_md || "No report generated.";

  // Hotspot Tiles
  const hsGrid = document.getElementById("hotspot-grid");
  const topTargets = DATA.targets || [];
  const maxRoi = Math.max(1, ...topTargets.map(t => t.roi_score || 0));
  for (const t of topTargets.slice(0, 12)) {
    const ratio = Math.min(1, (t.roi_score || 0) / maxRoi);
    const tile = document.createElement("div");
    tile.className = "hs-tile";
    tile.style.background = `rgba(239, 68, 68, ${(0.12 + ratio * 0.42).toFixed(2)})`;
    tile.innerHTML = `<div style="font-weight:700">${esc(t.name)}</div>
      <div style="color:var(--muted);font-size:11px">${esc(t.file_path)}:${t.start_line}</div>
      <div style="margin-top:4px">ROI <b>${t.roi_score}</b> · C=${t.complexity} · Churn=${t.churn_90d}</div>`;
    tile.onclick = () => showNodeDetail(t);
    hsGrid.appendChild(tile);
  }

  function showNodeDetail(t) {
    const el = document.getElementById("node-detail");
    const fileSmells = (DATA.smells || []).filter(s => s.file_path === t.file_path);
    el.innerHTML = `
      <h3 style="margin:6px 0">${esc(t.name)} <span class="pill lang-${esc(t.language)}">${esc(t.language)}</span></h3>
      <div style="font-size:12px;color:var(--muted)">${esc(t.service_name || "")} · ${esc(t.file_path)}:${t.start_line}-${t.end_line || t.start_line}</div>
      <div style="margin:8px 0;font-size:12px">ROI: <b>${t.roi_score}</b> | Complexity: <b>${t.complexity}</b> | Fan-In: <b>${t.fan_in}</b> | Fan-Out: <b>${t.fan_out}</b> | Clones: <b>${t.clone_count}</b></div>
      ${fileSmells.length ? `<div style="margin:6px 0"><b>File Smells:</b><ul>${fileSmells.slice(0,6).map(s=>`<li><span class="pill sev-${esc(s.severity)}">${esc(s.rule_id)}</span> L${s.line}: ${esc(s.message)}</li>`).join("")}</ul></div>` : ""}
      <pre>${esc(t.clean_body || "(body omitted)")}</pre>`;
  }

  // Sortable & filterable ROI Table
  let sortCol = "roi_score", sortDesc = true;
  const tbody = document.querySelector("#roi-table tbody");
  const searchBox = document.getElementById("search-box");
  const langFilter = document.getElementById("lang-filter");

  function renderTable() {
    const q = (searchBox.value || "").toLowerCase();
    const lf = langFilter.value;
    const rows = topTargets.filter(t => {
      if (lf && t.language !== lf) return false;
      if (q && !(`${t.name} ${t.file_path} ${t.service_name || ""}`).toLowerCase().includes(q)) return false;
      return true;
    }).sort((a, b) => {
      const av = a[sortCol], bv = b[sortCol];
      if (typeof av === "number" && typeof bv === "number") return sortDesc ? bv - av : av - bv;
      return sortDesc ? String(bv).localeCompare(String(av)) : String(av).localeCompare(String(bv));
    });
    tbody.innerHTML = "";
    for (const t of rows.slice(0, 100)) {
      const tr = document.createElement("tr");
      tr.style.cursor = "pointer";
      tr.innerHTML = `<td><b>${t.roi_score}</b></td>
        <td>${esc(t.service_name || "-")}</td>
        <td><span class="pill lang-${esc(t.language)}">${esc(t.language)}</span></td>
        <td>${esc(t.name)}</td>
        <td style="color:var(--muted)">${esc(t.file_path)}:${t.start_line}</td>
        <td>${t.complexity}</td><td>${t.churn_90d}</td><td>${t.fan_in}</td>
        <td>${t.smell_count}</td><td>${t.clone_count}</td>`;
      tr.onclick = () => showNodeDetail(t);
      tbody.appendChild(tr);
    }
  }
  document.querySelectorAll("#roi-table th").forEach(th => {
    th.onclick = () => {
      const col = th.dataset.col;
      if (sortCol === col) sortDesc = !sortDesc;
      else { sortCol = col; sortDesc = true; }
      renderTable();
    };
  });
  searchBox.oninput = renderTable;
  langFilter.onchange = renderTable;
  renderTable();

  // Clones & Smells / Boundaries
  const cs = document.getElementById("clones-smells");
  const clones = DATA.clones || [];
  const boundaries = DATA.boundaries || [];
  const smells = DATA.smells || [];
  cs.innerHTML = `
    <h3 style="margin:4px 0 8px;font-size:13px">Semantic Clones (${clones.length})</h3>
    <table><thead><tr><th>Sim</th><th>Symbol A</th><th>Symbol B</th></tr></thead><tbody>
      ${clones.slice(0, 30).map(c => `<tr><td><b>${c.similarity}</b></td><td>${esc(c.a_service || "")} ${esc(c.a_name)}</td><td>${esc(c.b_service || "")} ${esc(c.b_name)}</td></tr>`).join("") || "<tr><td colspan=3 style='color:var(--muted)'>None above threshold</td></tr>"}
    </tbody></table>
    <h3 style="margin:14px 0 8px;font-size:13px">Boundary Contracts (${boundaries.length})</h3>
    <table><thead><tr><th>Dir</th><th>Proto</th><th>Identifier</th><th>Location</th></tr></thead><tbody>
      ${boundaries.slice(0, 40).map(b => `<tr><td>${esc(b.direction)}</td><td>${esc(b.protocol)}</td><td>${esc(b.identifier)}</td><td style="color:var(--muted)">${esc(b.file_path)}:${b.line}</td></tr>`).join("") || "<tr><td colspan=4 style='color:var(--muted)'>None extracted</td></tr>"}
    </tbody></table>
    <h3 style="margin:14px 0 8px;font-size:13px">Structural Smells (${smells.length})</h3>
    <table><thead><tr><th>Rule</th><th>Sev</th><th>Location</th><th>Message</th></tr></thead><tbody>
      ${smells.slice(0, 40).map(s => `<tr><td><span class="pill sev-${esc(s.severity)}">${esc(s.rule_id)}</span></td><td>${esc(s.severity)}</td><td>${esc(s.file_path)}:${s.line}</td><td>${esc(s.message)}</td></tr>`).join("") || "<tr><td colspan=4 style='color:var(--muted)'>No smells matched</td></tr>"}
    </tbody></table>`;

  // Force-Directed Canvas Graph
  const canvas = document.getElementById("graph-canvas");
  const ctx = canvas.getContext("2d");
  const gNodes = (DATA.graph_nodes || []).map((n, idx) => ({
    ...n,
    x: 100 + ((idx * 73) % 520),
    y: 70 + ((idx * 137) % 330),
    vx: 0, vy: 0,
  }));
  const nodeById = new Map(gNodes.map(n => [n.id, n]));
  const gEdges = (DATA.graph_edges || []).filter(e => nodeById.has(e.source) && nodeById.has(e.target));
  const edgeFilter = document.getElementById("edge-filter");
  const langColor = { rust: "#fb923c", java: "#60a5fa", go: "#2dd4bf", boundary: "#f59e0b" };

  function resize() {
    const r = canvas.getBoundingClientRect();
    canvas.width = Math.max(400, Math.floor(r.width));
    canvas.height = 470;
  }
  resize();
  window.addEventListener("resize", resize);

  let dragNode = null, hoverNode = null;
  function tick() {
    const W = canvas.width, H = canvas.height;
    const ef = edgeFilter.value;
    // Repulsion
    for (let i = 0; i < gNodes.length; i++) {
      const a = gNodes[i];
      a.vx += (W / 2 - a.x) * 0.0015;
      a.vy += (H / 2 - a.y) * 0.0015;
      for (let j = i + 1; j < gNodes.length; j++) {
        const b = gNodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy + 25;
        let f = 420 / d2;
        a.vx += dx * f; a.vy += dy * f;
        b.vx -= dx * f; b.vy -= dy * f;
      }
    }
    // Springs
    for (const e of gEdges) {
      if (ef !== "ALL" && e.type !== ef) continue;
      const a = nodeById.get(e.source), b = nodeById.get(e.target);
      const dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.hypot(dx, dy) || 1;
      const targetDist = e.type === "CLONE" ? 95 : 120;
      const f = (dist - targetDist) * 0.012;
      a.vx += (dx / dist) * f; a.vy += (dy / dist) * f;
      b.vx -= (dx / dist) * f; b.vy -= (dy / dist) * f;
    }
    for (const n of gNodes) {
      if (n !== dragNode) {
        n.vx *= 0.84; n.vy *= 0.84;
        n.x = Math.max(24, Math.min(W - 24, n.x + n.vx));
        n.y = Math.max(24, Math.min(H - 24, n.y + n.vy));
      }
    }
    // Draw
    ctx.clearRect(0, 0, W, H);
    for (const e of gEdges) {
      if (ef !== "ALL" && e.type !== ef) continue;
      const a = nodeById.get(e.source), b = nodeById.get(e.target);
      ctx.save();
      ctx.beginPath();
      if (e.type === "CLONE") {
        ctx.setLineDash([5, 4]);
        ctx.strokeStyle = "rgba(56, 189, 248, 0.72)";
        ctx.lineWidth = 1.6;
      } else if (e.type === "CO_CHANGES_WITH") {
        ctx.setLineDash([2, 3]);
        ctx.strokeStyle = "rgba(245, 158, 11, 0.55)";
        ctx.lineWidth = 1.4;
      } else if (e.type === "BOUNDARY") {
        ctx.strokeStyle = "rgba(245, 158, 11, 0.45)";
        ctx.lineWidth = 1.2;
      } else {
        ctx.strokeStyle = "rgba(148, 163, 184, 0.42)";
        ctx.lineWidth = 1.2;
      }
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
      ctx.restore();
    }
    for (const n of gNodes) {
      const r = Math.max(5, Math.min(18, 5 + (n.hotspot || 0) * 1.4));
      if (n.smells > 0) {
        ctx.beginPath();
        ctx.arc(n.x, n.y, r + 3.5, 0, Math.PI * 2);
        ctx.strokeStyle = "rgba(239, 68, 68, 0.85)";
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.fillStyle = langColor[n.lang] || "#94a3b8";
      ctx.fill();
      if (n === hoverNode || n === dragNode || r >= 10) {
        ctx.font = "11px ui-sans-serif, system-ui";
        ctx.fillStyle = "#e6ecf8";
        ctx.fillText(n.label, n.x + r + 4, n.y + 4);
      }
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  function hitTest(mx, my) {
    for (let i = gNodes.length - 1; i >= 0; i--) {
      const n = gNodes[i];
      if (Math.hypot(n.x - mx, n.y - my) <= 16) return n;
    }
    return null;
  }
  canvas.addEventListener("mousedown", e => {
    const rect = canvas.getBoundingClientRect();
    dragNode = hitTest(e.clientX - rect.left, e.clientY - rect.top);
    if (dragNode && dragNode.target) showNodeDetail(dragNode.target);
  });
  canvas.addEventListener("mousemove", e => {
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    hoverNode = hitTest(mx, my);
    if (dragNode) { dragNode.x = mx; dragNode.y = my; }
  });
  window.addEventListener("mouseup", () => { dragNode = null; });
})();
</script>
</body>
</html>
"""


def _render_html(title: str, subtitle: str, payload: dict[str, Any]) -> str:
    safe_json = json.dumps(payload, ensure_ascii=False).replace("</script>", "<\\/script>")
    return (
        _DASHBOARD_TEMPLATE.replace("__TITLE__", html.escape(title))
        .replace("__SUBTITLE__", html.escape(subtitle))
        .replace("__JSON_PAYLOAD__", safe_json)
    )


def generate_service_dashboard(capsule_dir: Path, out_html: Path | None = None) -> Path:
    manifest = json.loads((capsule_dir / "manifest.json").read_text(encoding="utf-8"))
    ranked_path = capsule_dir / "ranked_targets.json"
    targets = json.loads(ranked_path.read_text(encoding="utf-8")) if ranked_path.is_file() else []
    smells = json.loads((capsule_dir / "smells.json").read_text(encoding="utf-8")) if (capsule_dir / "smells.json").is_file() else []
    boundaries = json.loads((capsule_dir / "boundaries.json").read_text(encoding="utf-8")) if (capsule_dir / "boundaries.json").is_file() else []
    clones = json.loads((capsule_dir / "semantic_clones.json").read_text(encoding="utf-8")) if (capsule_dir / "semantic_clones.json").is_file() else []
    report_md = (capsule_dir / "refactor_report.md").read_text(encoding="utf-8") if (capsule_dir / "refactor_report.md").is_file() else ""

    con = duckdb.connect()
    edges_pq = capsule_dir / "edges.parquet"
    raw_edges = []
    if edges_pq.is_file():
        raw_edges = con.execute("SELECT source_id, target_id, edge_type, weight FROM read_parquet(?) LIMIT 300", [str(edges_pq)]).fetchall()

    smell_by_file: dict[str, int] = {}
    for s in smells:
        smell_by_file[s.get("file_path", "")] = smell_by_file.get(s.get("file_path", ""), 0) + 1

    graph_nodes = []
    seen_ids = set()
    for t in targets[:70]:
        sid = t["symbol_id"]
        seen_ids.add(sid)
        graph_nodes.append({
            "id": sid,
            "label": t["name"],
            "lang": t["language"],
            "hotspot": t.get("hotspot_score", 1.0),
            "smells": smell_by_file.get(t["file_path"], 0),
            "target": t,
        })
    for b in boundaries[:25]:
        bid = f"bnd:{b['protocol']}:{b['identifier']}"
        if bid not in seen_ids:
            seen_ids.add(bid)
            graph_nodes.append({"id": bid, "label": b["identifier"][:28], "lang": "boundary", "hotspot": 2.0, "smells": 0})

    graph_edges = [
        {"source": r[0], "target": r[1], "type": r[2], "weight": float(r[3])}
        for r in raw_edges
        if r[0] in seen_ids and r[1] in seen_ids
    ]
    for c in clones[:30]:
        if c["a_id"] in seen_ids and c["b_id"] in seen_ids:
            graph_edges.append({"source": c["a_id"], "target": c["b_id"], "type": "CLONE", "weight": c["similarity"]})

    # Link boundaries to targets in the same file
    target_by_file: dict[str, str] = {}
    for t in targets[:70]:
        target_by_file.setdefault(t["file_path"], t["symbol_id"])
    for b in boundaries[:25]:
        bid = f"bnd:{b['protocol']}:{b['identifier']}"
        tid = target_by_file.get(b["file_path"])
        if tid and bid in seen_ids:
            graph_edges.append({"source": tid, "target": bid, "type": "BOUNDARY", "weight": 1.0})

    payload = {
        "kpis": [
            {"label": "Service", "value": manifest.get("service_name", "-")},
            {"label": "Symbols", "value": manifest.get("node_count", 0)},
            {"label": "Graph Edges", "value": manifest.get("edge_count", 0)},
            {"label": "Structural Smells", "value": manifest.get("smell_count", 0)},
            {"label": "Boundaries", "value": manifest.get("boundary_count", 0)},
            {"label": "Semantic Clones", "value": len(clones)},
            {"label": "Embed Engine", "value": manifest.get("embedding_backend", "-")},
            {"label": "LLM Engine", "value": manifest.get("llm_backend", "-")},
        ],
        "targets": targets,
        "smells": smells,
        "boundaries": boundaries,
        "clones": clones,
        "report_md": report_md,
        "graph_nodes": graph_nodes,
        "graph_edges": graph_edges,
    }
    dest = out_html or (capsule_dir / "dashboard.html")
    dest.write_text(
        _render_html(
            f"Refactor Radar — {manifest.get('service_name', 'Service')}",
            f"Commit {manifest.get('commit_sha', '')[:12]} · Languages: {', '.join(manifest.get('languages', []))}",
            payload,
        ),
        encoding="utf-8",
    )
    return dest


def generate_federation_dashboard(
    capsules_root: Path,
    federated_dir: Path,
    summary: dict[str, Any],
    report_md: str,
) -> Path:
    all_targets: list[dict[str, Any]] = []
    all_smells: list[dict[str, Any]] = []
    all_boundaries: list[dict[str, Any]] = []

    for mf in sorted(capsules_root.glob("*/manifest.json")):
        cap = mf.parent
        svc = mf.parent.name
        rt = cap / "ranked_targets.json"
        if rt.is_file():
            for item in json.loads(rt.read_text(encoding="utf-8"))[:20]:
                item.setdefault("service_name", svc)
                all_targets.append(item)
        sf = cap / "smells.json"
        if sf.is_file():
            all_smells.extend(json.loads(sf.read_text(encoding="utf-8"))[:30])
        bf = cap / "boundaries.json"
        if bf.is_file():
            all_boundaries.extend(json.loads(bf.read_text(encoding="utf-8"))[:30])

    all_targets.sort(key=lambda x: x.get("roi_score", 0), reverse=True)
    cross_clones = summary.get("cross_service_polyglot_clones", [])

    graph_nodes = []
    seen_ids = set()
    for t in all_targets[:60]:
        sid = t["symbol_id"]
        if sid not in seen_ids:
            seen_ids.add(sid)
            graph_nodes.append({
                "id": sid,
                "label": f"{t.get('service_name','')}:{t['name']}",
                "lang": t["language"],
                "hotspot": t.get("hotspot_score", 1.0),
                "smells": t.get("smell_count", 0),
                "target": t,
            })

    graph_edges = []
    for c in cross_clones[:40]:
        # ensure both endpoints exist in graph
        for side_id, side_svc, side_name, side_lang in (
            (c.get("a_id"), c.get("service_a"), c.get("fn_a"), c.get("lang_a")),
            (c.get("b_id"), c.get("service_b"), c.get("fn_b"), c.get("lang_b")),
        ):
            if side_id and side_id not in seen_ids:
                seen_ids.add(side_id)
                graph_nodes.append({
                    "id": side_id,
                    "label": f"{side_svc}:{str(side_name).split(':')[-1]}",
                    "lang": side_lang or "rust",
                    "hotspot": 3.0,
                    "smells": 0,
                })
        if c.get("a_id") in seen_ids and c.get("b_id") in seen_ids:
            graph_edges.append({"source": c["a_id"], "target": c["b_id"], "type": "CLONE", "weight": c["similarity"]})

    # Convert cross clones to standard clone table shape
    clones_fmt = [
        {
            "a_service": c["service_a"],
            "a_name": f"[{c['lang_a']}] {c['fn_a']}",
            "b_service": c["service_b"],
            "b_name": f"[{c['lang_b']}] {c['fn_b']}",
            "similarity": c["similarity"],
        }
        for c in cross_clones
    ]

    payload = {
        "kpis": [
            {"label": "Services Federated", "value": len(summary.get("analyzed_services", []))},
            {"label": "Total Symbols", "value": summary.get("total_nodes", 0)},
            {"label": "Total Edges", "value": summary.get("total_edges", 0)},
            {"label": "Cross-Service Clones", "value": len(cross_clones)},
            {"label": "Shared Contracts/Tables", "value": len(summary.get("shared_contracts_or_tables", {}))},
            {"label": "Distributed Tickets", "value": len(summary.get("distributed_monolith_tickets", []))},
        ],
        "targets": all_targets[:80],
        "smells": all_smells[:80],
        "boundaries": all_boundaries[:80],
        "clones": clones_fmt,
        "report_md": report_md,
        "graph_nodes": graph_nodes,
        "graph_edges": graph_edges,
    }
    dest = federated_dir / "ecosystem_dashboard.html"
    dest.write_text(
        _render_html(
            "Refactor Radar — Ecosystem Federation Dashboard",
            f"Services: {', '.join(summary.get('analyzed_services', []))}",
            payload,
        ),
        encoding="utf-8",
    )
    return dest

